"""WASAPI loopback capture through ctypes (no extra audio package).

Two kinds of sources:
  * process loopback: only the audio rendered by one process tree (e.g. Teams),
    independent of the playback device (Windows 10 2004+/Windows 11);
  * device loopback: everything played on one render endpoint.

Output: 16 kHz mono float32 blocks handed to a callback. WASAPI delivers no
packets while nothing plays, so silence is filled from the wall clock to keep
the timeline continuous (the VAD needs to see the pause to close a sentence).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import os
import threading
import time
from dataclasses import dataclass

import numpy as np

TARGET_RATE = 16000

ole32 = ctypes.windll.ole32
kernel32 = ctypes.windll.kernel32
HRESULT = ctypes.c_long
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __eq__(self, other):
        return bytes(self) == bytes(other)


def _guid(text: str) -> GUID:
    g = GUID()
    hr = ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    if hr < 0:
        raise OSError(f"bad GUID {text}")
    return g


CLSID_MMDeviceEnumerator = _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioClient = _guid("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = _guid("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
IID_IUnknown = _guid("{00000000-0000-0000-C000-000000000046}")
IID_IAgileObject = _guid("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
IID_ICompletionHandler = _guid("{41D949AB-9862-444A-80F6-C261334DA5EB}")
PKEY_FriendlyName_fmtid = _guid("{a45c254e-df1c-4efd-8020-67d146a850e0}")
SUBTYPE_PCM = _guid("{00000001-0000-0010-8000-00aa00389b71}")
SUBTYPE_FLOAT = _guid("{00000003-0000-0010-8000-00aa00389b71}")

CLSCTX_ALL = 23
COINIT_MULTITHREADED = 0
E_NOINTERFACE = -2147467262
eRender, eConsole, eMultimedia = 0, 0, 1
DEVICE_STATE_ACTIVE = 1
STREAMFLAGS_LOOPBACK = 0x00020000
STREAMFLAGS_EVENTCALLBACK = 0x00040000
STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
BUFFERFLAGS_SILENT = 0x2
AUDCLNT_E_DEVICE_INVALIDATED = -2004287484  # 0x88890004
WAVE_FORMAT_PCM, WAVE_FORMAT_FLOAT, WAVE_FORMAT_EXTENSIBLE = 1, 3, 0xFFFE


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wt.WORD), ("nChannels", wt.WORD), ("nSamplesPerSec", wt.DWORD), ("nAvgBytesPerSec", wt.DWORD),
                ("nBlockAlign", wt.WORD), ("wBitsPerSample", wt.WORD), ("cbSize", wt.WORD)]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("Format", WAVEFORMATEX), ("wValidBitsPerSample", wt.WORD), ("dwChannelMask", wt.DWORD), ("SubFormat", GUID)]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wt.ULONG), ("pBlobData", ctypes.c_void_p)]


class _PVUnion(ctypes.Union):
    _fields_ = [("blob", BLOB), ("pwszVal", ctypes.c_wchar_p), ("ulVal", wt.ULONG)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort), ("u", _PVUnion)]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wt.DWORD)]


class ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", wt.DWORD), ("TargetProcessId", wt.DWORD), ("ProcessLoopbackMode", wt.DWORD)]


def _check(hr: int, what: str):
    if hr < 0:
        raise OSError(hr & 0xFFFFFFFF, f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    return hr


def _method(ptr, index: int, *argtypes, restype=HRESULT):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = proto(vtbl[index])
    return lambda *args: fn(ptr, *args)


def _release(ptr):
    if ptr:
        _method(ptr, 2, restype=ctypes.c_ulong)()


def _co_init():
    hr = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    return hr >= 0  # S_FALSE (already initialised) also needs CoUninitialize


# ---------------------------------------------------------------- processes
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def list_processes() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) for every running process."""
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out = []
    try:
        ok = kernel32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snap))
    return out


def find_root_process(names: list[str]) -> int | None:
    """Oldest root of a process family, e.g. the main ms-teams.exe whose children play the call audio."""
    wanted = {n.lower() for n in names}
    procs = list_processes()
    family = {pid: ppid for pid, ppid, exe in procs if exe.lower() in wanted}
    roots = [pid for pid, ppid in family.items() if ppid not in family]
    return min(roots) if roots else None


def process_alive(pid: int) -> bool:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wt.DWORD()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


# ---------------------------------------------------------------- devices
@dataclass
class RenderDevice:
    id: str
    name: str
    default: bool


def _enumerator():
    ptr = ctypes.c_void_p()
    _check(ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL, ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(ptr)), "CoCreateInstance(MMDeviceEnumerator)")
    return ptr


def _device_id(device) -> str:
    pid = ctypes.c_void_p()
    _check(_method(device, 5, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(pid)), "IMMDevice.GetId")
    try:
        return ctypes.wstring_at(pid)
    finally:
        ole32.CoTaskMemFree(pid)


def _device_name(device) -> str:
    store = ctypes.c_void_p()
    _check(_method(device, 4, wt.DWORD, ctypes.POINTER(ctypes.c_void_p))(0, ctypes.byref(store)), "OpenPropertyStore")
    try:
        key = PROPERTYKEY(PKEY_FriendlyName_fmtid, 14)
        value = PROPVARIANT()
        _check(_method(store, 5, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(ctypes.byref(key), ctypes.byref(value)), "GetValue")
        try:
            return (value.u.pwszVal or "?") if value.vt == 31 else "?"
        finally:
            ole32.PropVariantClear(ctypes.byref(value))
    finally:
        _release(store)


def list_render_devices() -> list[RenderDevice]:
    inited = _co_init()
    try:
        enum = _enumerator()
        try:
            default_id = None
            dev = ctypes.c_void_p()
            if _method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(eRender, eMultimedia, ctypes.byref(dev)) >= 0:
                default_id = _device_id(dev)
                _release(dev)
            coll = ctypes.c_void_p()
            _check(_method(enum, 3, ctypes.c_int, wt.DWORD, ctypes.POINTER(ctypes.c_void_p))(eRender, DEVICE_STATE_ACTIVE, ctypes.byref(coll)), "EnumAudioEndpoints")
            try:
                count = ctypes.c_uint()
                _check(_method(coll, 3, ctypes.POINTER(ctypes.c_uint))(ctypes.byref(count)), "GetCount")
                out = []
                for i in range(count.value):
                    d = ctypes.c_void_p()
                    _check(_method(coll, 4, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(i, ctypes.byref(d)), "Item")
                    try:
                        did = _device_id(d)
                        out.append(RenderDevice(did, _device_name(d), did == default_id))
                    finally:
                        _release(d)
                out.sort(key=lambda r: (not r.default, r.name))
                return out
            finally:
                _release(coll)
        finally:
            _release(enum)
    finally:
        if inited:
            ole32.CoUninitialize()


# ---------------------------------------------------------------- activation
_QI = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_ADDREF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_COMPLETED = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _QI), ("AddRef", _ADDREF), ("Release", _ADDREF), ("ActivateCompleted", _COMPLETED)]


class _Handler(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_HandlerVtbl))]


class _CompletionHandler:
    """Minimal agile COM object for ActivateAudioInterfaceAsync."""

    def __init__(self):
        self.done = threading.Event()
        self.refs = 1

        def qi(this, riid, ppv):
            iid = riid.contents
            if iid == IID_IUnknown or iid == IID_ICompletionHandler or iid == IID_IAgileObject:
                ppv[0] = this
                self.refs += 1
                return 0
            ppv[0] = None
            return E_NOINTERFACE

        def addref(this):
            self.refs += 1
            return self.refs

        def release(this):
            self.refs -= 1
            return max(self.refs, 0)

        def completed(this, op):
            self.done.set()
            return 0

        self._fns = (_QI(qi), _ADDREF(addref), _ADDREF(release), _COMPLETED(completed))
        self.vtbl = _HandlerVtbl(*self._fns)
        self.obj = _Handler(ctypes.pointer(self.vtbl))

    @property
    def ptr(self):
        return ctypes.addressof(self.obj)


def _activate_process_loopback(pid: int, *, exclude=False):
    params = ACTIVATION_PARAMS(1, pid, int(exclude))  # PROCESS_LOOPBACK_MODE_INCLUDE / EXCLUDE_TARGET_PROCESS_TREE
    pv = PROPVARIANT()
    pv.vt = 65  # VT_BLOB
    pv.u.blob.cbSize = ctypes.sizeof(params)
    pv.u.blob.pBlobData = ctypes.addressof(params)
    handler = _CompletionHandler()
    op = ctypes.c_void_p()
    fn = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
    fn.restype = HRESULT
    fn.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID), ctypes.POINTER(PROPVARIANT), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _check(fn("VAD\\Process_Loopback", ctypes.byref(IID_IAudioClient), ctypes.byref(pv), ctypes.c_void_p(handler.ptr), ctypes.byref(op)), "ActivateAudioInterfaceAsync")
    try:
        if not handler.done.wait(10):
            raise TimeoutError("process loopback activation timed out")
        hr = HRESULT()
        unk = ctypes.c_void_p()
        _check(_method(op, 3, ctypes.POINTER(HRESULT), ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(hr), ctypes.byref(unk)), "GetActivateResult")
        _check(hr.value, "process loopback activation")
        return unk  # IAudioClient
    finally:
        _release(op)
        del handler


def _activate_device(device_id: str | None):
    enum = _enumerator()
    try:
        dev = ctypes.c_void_p()
        if device_id:
            _check(_method(enum, 5, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p))(device_id, ctypes.byref(dev)), "GetDevice")
        else:
            _check(_method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(eRender, eMultimedia, ctypes.byref(dev)), "GetDefaultAudioEndpoint")
        try:
            name = _device_name(dev)
            client = ctypes.c_void_p()
            _check(_method(dev, 3, ctypes.POINTER(GUID), wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None, ctypes.byref(client)), "IMMDevice.Activate")
            return client, name
        finally:
            _release(dev)
    finally:
        _release(enum)


# ---------------------------------------------------------------- conversion
class StreamResampler:
    """Chunk-boundary-safe polyphase resampler (keeps filter context between packets)."""

    def __init__(self, src_rate: int, dst_rate: int = TARGET_RATE):
        g = math.gcd(src_rate, dst_rate)
        self.up, self.down = dst_rate // g, src_rate // g
        radius = math.ceil(10 * max(self.up, self.down) / self.up)
        self.margin = self.down * math.ceil(max(64, radius) / self.down)
        self.buf = np.zeros(self.margin, np.float32)
        if (self.up, self.down) != (1, 1):
            from scipy.signal import firwin, resample_poly
            self._resample = resample_poly
            # Same float32 Kaiser low-pass as resample_poly's default. A WASAPI
            # session has one sample rate, so design the FIR once for all packets.
            rate = max(self.up, self.down)
            self.taps = firwin(20 * rate + 1, 1.0 / rate, window=("kaiser", 5.0)).astype(np.float32)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.up == self.down:
            return x
        self.buf = np.concatenate([self.buf, x])
        n = ((len(self.buf) - 2 * self.margin) // self.down) * self.down
        if n <= 0:
            return np.zeros(0, np.float32)
        y = self._resample(self.buf[: n + 2 * self.margin], self.up, self.down, window=self.taps)
        o0 = self.margin // self.down * self.up
        out = y[o0 : o0 + n // self.down * self.up].astype(np.float32, copy=False)
        self.buf = self.buf[n:]
        return out


def _decoder(fmt: WAVEFORMATEX, ext: WAVEFORMATEXTENSIBLE | None):
    tag = fmt.wFormatTag
    is_float = tag == WAVE_FORMAT_FLOAT or (tag == WAVE_FORMAT_EXTENSIBLE and ext is not None and ext.SubFormat == SUBTYPE_FLOAT)
    bits, ch = fmt.wBitsPerSample, fmt.nChannels

    def decode(raw: bytes) -> np.ndarray:
        if is_float and bits == 32:
            a = np.frombuffer(raw, "<f4")
        elif bits == 16:
            a = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
        elif bits == 32:
            a = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
        elif bits == 24:
            b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
            v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
            a = (np.where(v >= 1 << 23, v - (1 << 24), v)).astype(np.float32) / 8388608.0
        else:
            raise ValueError(f"unsupported sample format: {bits} bit")
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        return a.astype(np.float32, copy=False)

    return decode


# ---------------------------------------------------------------- capture
@dataclass
class SourceSpec:
    kind: str  # "system" | "process" | "device"
    process_names: tuple[str, ...] = ()
    device_id: str | None = None  # None = default playback device
    label: str = ""
    pid: int | None = None  # fixed process (tests); otherwise found by name


class LoopbackCapture:
    """Background thread delivering 16 kHz mono float32 to ``on_audio``.

    ``on_status(text, ok)`` reports what is being captured or why it failed.
    Recovers automatically when the device is switched/removed or the target
    process restarts.
    """

    def __init__(self, spec: SourceSpec, on_audio, on_status=None, fallback_device=True):
        self.spec = spec
        self.on_audio = on_audio
        self.on_status = on_status or (lambda text, ok: None)
        self.fallback_device = fallback_device
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.active_label = ""
        self.stats = {"packets": 0, "raw_frames": 0, "silent_packets": 0, "padded": 0, "delivered": 0, "rate": 0}

    def start(self):
        self._thread = threading.Thread(target=self._run, name="loopback-capture", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    # -- internal
    def _open(self):
        """Returns (client, label, pid or None)."""
        spec = self.spec
        if spec.kind == "system":
            pid = os.getpid()
            try:
                return _activate_process_loopback(pid, exclude=True), "全电脑声音（所有播放设备）", pid
            except OSError:
                client, name = _activate_device(None)
                return client, f"系统回环不可用，正在监听默认播放设备 · {name}", None
        if spec.kind == "process":
            pid = spec.pid or find_root_process(list(spec.process_names))
            if pid is not None:
                return _activate_process_loopback(pid), f"{spec.label}（进程 {pid}）", pid
            if not self.fallback_device:
                raise LookupError(f"未找到 {spec.label} 进程")
            client, name = _activate_device(None)
            return client, f"默认播放设备：{name}（未检测到 {spec.label}，自动切换）", None
        client, name = _activate_device(spec.device_id)
        return client, f"播放设备：{name}", None

    def _run(self):
        inited = _co_init()
        try:
            while not self._stop.is_set():
                try:
                    self._session()
                except Exception as exc:  # report and retry; a meeting must survive device changes
                    self.on_status(f"音频采集中断：{exc}；2 秒后重试", False)
                    self._stop.wait(2)
        finally:
            if inited:
                ole32.CoUninitialize()

    def _session(self):
        client, label, pid = self._open()
        capture = ctypes.c_void_p()
        event = kernel32.CreateEventW(None, False, False, None)
        mix = ctypes.POINTER(WAVEFORMATEX)()
        try:
            if pid is not None:
                fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, 2, 48000, 48000 * 4, 4, 16, 0)
                ext = None
                flags = STREAMFLAGS_LOOPBACK | STREAMFLAGS_EVENTCALLBACK | STREAMFLAGS_AUTOCONVERTPCM
                fmt_ptr = ctypes.byref(fmt)
            else:
                _check(_method(client, 8, ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)))(ctypes.byref(mix)), "GetMixFormat")
                fmt = mix.contents
                ext = ctypes.cast(mix, ctypes.POINTER(WAVEFORMATEXTENSIBLE)).contents if fmt.wFormatTag == WAVE_FORMAT_EXTENSIBLE else None
                flags = STREAMFLAGS_LOOPBACK | STREAMFLAGS_EVENTCALLBACK
                fmt_ptr = mix
            _check(_method(client, 3, ctypes.c_int, wt.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p)(
                0, flags, 2_000_000, 0, fmt_ptr, None), "IAudioClient.Initialize")
            _check(_method(client, 13, ctypes.c_void_p)(ctypes.c_void_p(event)), "SetEventHandle")
            _check(_method(client, 14, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(capture)), "GetService")
            rate, block = fmt.nSamplesPerSec, fmt.nBlockAlign
            self.stats["rate"] = rate
            decode = _decoder(fmt, ext)
            resample = StreamResampler(rate)
            get_next = _method(capture, 5, ctypes.POINTER(ctypes.c_uint32))
            get_buffer = _method(capture, 3, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(wt.DWORD), ctypes.c_void_p, ctypes.c_void_p)
            release_buffer = _method(capture, 4, ctypes.c_uint32)
            _check(_method(client, 10)(), "IAudioClient.Start")
            self.active_label = label
            self.on_status(f"正在采集：{label}", True)
            t0 = time.perf_counter()
            delivered = 0  # samples at TARGET_RATE handed to on_audio
            last_check = t0
            data = ctypes.c_void_p()
            frames = ctypes.c_uint32()
            flags_out = wt.DWORD()
            nxt = ctypes.c_uint32()
            while not self._stop.is_set():
                kernel32.WaitForSingleObject(ctypes.c_void_p(event), 20)
                got = False
                while True:
                    hr = get_next(ctypes.byref(nxt))
                    if hr == AUDCLNT_E_DEVICE_INVALIDATED:
                        raise OSError("播放设备已更改或移除")
                    _check(hr, "GetNextPacketSize")
                    if nxt.value == 0:
                        break
                    _check(get_buffer(ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags_out), None, None), "GetBuffer")
                    n = frames.value
                    self.stats["packets"] += 1
                    self.stats["raw_frames"] += n
                    if flags_out.value & BUFFERFLAGS_SILENT:
                        self.stats["silent_packets"] += 1
                    if flags_out.value & BUFFERFLAGS_SILENT or not data.value:
                        raw = bytes(n * block)
                    else:
                        raw = ctypes.string_at(data.value, n * block)
                    release_buffer(n)
                    out = resample(decode(raw))
                    if len(out):
                        delivered += len(out)
                        self.stats["delivered"] = delivered
                        self.on_audio(out)
                    got = True
                now = time.perf_counter()
                lag = (now - t0) * TARGET_RATE - delivered
                if not got and lag > 0.15 * TARGET_RATE:
                    pad = int(lag - 0.05 * TARGET_RATE)
                    delivered += pad
                    self.stats["padded"] += pad
                    self.on_audio(np.zeros(pad, np.float32))
                if now - last_check > 3:
                    last_check = now
                    if pid is not None and not process_alive(pid):
                        raise OSError("目标程序已退出，正在重新查找")
                    if pid is None and self.spec.kind == "process" and find_root_process(list(self.spec.process_names)):
                        self.on_status(f"检测到 {self.spec.label}，切换为只采集该程序声音", True)
                        return  # reopen as process loopback
            _method(client, 11)()
        finally:
            if mix:
                ole32.CoTaskMemFree(mix)
            _release(capture)
            _release(client)
            kernel32.CloseHandle(ctypes.c_void_p(event))
