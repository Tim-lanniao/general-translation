"""Real-time German -> Chinese caption engine.

Pipeline (parameters from the author's real-time replay evaluation, 2026-11-16):
  loopback audio 16 kHz mono -> causal Silero VAD -> Whisper Turbo -> streaming
  translation. Short drafts use beam 1. A pause starts final-quality recognition
  of the uncommitted speech. Only the same uninterrupted pause can confirm it.

Sentences longer than ~10 s are committed at a sentence/clause boundary taken
from Whisper word timestamps, so no speech falls out of the 12 s window.
No future audio is used for any caption.
"""
from __future__ import annotations

import collections
import dataclasses
import gc
import json
import logging
import re
import subprocess
import threading
import time
import wave
from concurrent.futures import CancelledError, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config
from .capture import LoopbackCapture, SourceSpec
from .glossary import Glossary

SR = 16000
log = logging.getLogger("teams_caption")

# Typical Whisper hallucinations on German silence/noise (TV subtitle credits etc.).
HALLUCINATION = re.compile(
    r"untertitel(ung)?\s+(im auftrag|von|der|des|durch)|amara\.org|vielen dank f(ü|ue)rs zuschauen|im auftrag des (zdf|wdr|swr|ndr|br)|"
    r"copyright\s+(wdr|zdf|swr)|^\W*(swr|zdf|wdr)\W*\d*\W*$", re.I)


# ---------------------------------------------------------------- audio store
class AudioRing:
    """Thread-safe store of the last ``keep_s`` seconds, addressed by absolute sample index."""

    def __init__(self, keep_s: int = 120):
        self.keep = keep_s * SR
        self.cap = self.keep * 2
        self.data = np.zeros(self.cap, np.float32)
        self.n = 0
        self.base = 0
        self.arrival = time.perf_counter()
        self.cond = threading.Condition()

    @property
    def total(self) -> int:
        return self.base + self.n

    def append(self, x: np.ndarray) -> None:
        with self.cond:
            if self.n + len(x) > self.cap:
                keep = min(self.n, self.keep)
                self.data[:keep] = self.data[self.n - keep : self.n]
                self.base += self.n - keep
                self.n = keep
            self.data[self.n : self.n + len(x)] = x
            self.n += len(x)
            self.arrival = time.perf_counter()
            self.cond.notify_all()

    def get(self, a: int, b: int) -> np.ndarray:
        with self.cond:
            a, b = max(a, self.base), min(b, self.total)
            return self.data[a - self.base : b - self.base].copy() if b > a else np.zeros(0, np.float32)

    def wall_of(self, index: int) -> float:
        """perf_counter time at which sample ``index`` arrived (approx.)."""
        return self.arrival - (self.total - index) / SR


# ---------------------------------------------------------------- causal VAD
class CausalVAD:
    """Frame-by-frame Silero VAD with the evaluation's thresholds (start >=.5, pause <.35)."""

    def __init__(self, ring: AudioRing, silence_ms: int, on_change, punctuated_ms: int = 384):
        from faster_whisper.vad import get_vad_model

        self.session = get_vad_model().session
        self.ring = ring
        self.silence_samples = round(silence_ms * 16)
        self.punctuated_samples = min(self.silence_samples, max(1, round(punctuated_ms * 16)))
        self.on_change = on_change
        self.lock = threading.Lock()
        self.finals: collections.deque = collections.deque()
        self.reset(ring.total)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="vad", daemon=True)

    def reset(self, position: int) -> None:
        with getattr(self, "lock", threading.Lock()):
            self.pos = position
            self.h = np.zeros((1, 1, 128), np.float32)
            self.c = self.h.copy()
            self.context = np.zeros((1, 64), np.float32)
            self.active: int | None = None
            self.silence: int | None = None
            self.punctuation_end = -1
            self.terminal = False
            self.last_commit = position
            self.last_speech = position
            self.seq = getattr(self, "seq", 0)
            self.finals.clear()
            self.last_speech_wall = time.perf_counter()

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        with self.ring.cond:
            self.ring.cond.notify_all()
        self.thread.join(timeout=2)

    def snapshot(self):
        with self.lock:
            return self.seq, self.active

    def note_transcript(self, seq, end, text):
        with self.lock:
            if self.active is not None and self.seq == seq and end >= self.punctuation_end:
                self.punctuation_end = end
                self.terminal = sentence_ended(text)

    def _run(self):
        while not self._stop.is_set():
            with self.ring.cond:
                if self.ring.total - self.pos < 512:
                    self.ring.cond.wait(0.1)
                    continue
            while self.ring.total - self.pos >= 512 and not self._stop.is_set():
                position = self.pos
                frame = self.ring.get(position, position + 512)
                if len(frame) < 512:  # fell out of the ring (should not happen)
                    with self.lock:
                        self.pos = max(self.pos, self.ring.base)
                    continue
                self._frame(position, frame.reshape(1, 512))

    def _frame(self, a: int, frame: np.ndarray):
        with self.lock:
            if self.pos != a:
                return
            context, h, c = self.context, self.h, self.c
        probs, h, c = self.session.run(None, {"input": np.concatenate([context, frame], axis=1), "h": h, "c": c})
        prob = float(np.asarray(probs).flat[0])
        end = a + 512
        changed = False
        with self.lock:
            if self.pos != a:  # pause or source switch while Silero was running
                return
            self.h, self.c, self.context = h, c, frame[:, -64:]
            if prob >= 0.5:
                self.last_speech = end
                self.last_speech_wall = time.perf_counter()
                changed = self.silence is not None
                self.silence = None
                if self.active is None:
                    self.active = max(self.last_commit, a - 3200)
                    self.seq += 1
                    changed = True
            elif self.active is not None:
                if prob < 0.35 and self.silence is None:
                    self.silence = a
                    changed = True
                elif prob >= 0.35 and self.silence is not None:
                    self.silence = None  # an uncertain frame invalidates the early decode too
                    changed = True
            if self.silence is not None:
                punctuated = self.terminal and self.punctuation_end >= self.silence
                threshold = self.punctuated_samples if punctuated else self.silence_samples
                if end - self.silence >= threshold and end - self.active >= SR:
                    self.finals.append({"seq": self.seq, "start": self.active, "end": end,
                                        "speech_end": self.last_speech, "gap": self.silence})
                    self.last_commit = end
                    self.active = None
                    self.silence = None
                    self.punctuation_end = -1
                    self.terminal = False
                    changed = True
            self.pos = end
        if changed:
            self.on_change()


# ---------------------------------------------------------------- sources
class FileSource:
    """Feeds a 16 kHz mono PCM16 WAV at real-time pace (testing without Teams)."""

    def __init__(self, path: str, on_audio, on_status=None, tail_s: float = 4.0, done=None):
        self.path, self.on_audio, self.on_status = path, on_audio, on_status or (lambda t, ok: None)
        self.tail_s, self.done = tail_s, done
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.active_label = f"文件回放：{Path(path).name}"
        self.stats = {}

    def start(self):
        self.thread = threading.Thread(target=self._run, name="file-source", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join()

    def _run(self):
        with wave.open(self.path, "rb") as f:
            if (f.getframerate(), f.getnchannels(), f.getsampwidth()) != (SR, 1, 2):
                raise ValueError("需要 16 kHz 单声道 16 位 WAV")
            audio = np.frombuffer(f.readframes(f.getnframes()), "<i2").astype(np.float32) / 32768
        audio = np.concatenate([audio, np.zeros(int(self.tail_s * SR), np.float32)])
        self.on_status(f"正在回放：{Path(self.path).name}", True)
        t0, pos, block = time.perf_counter(), 0, SR // 50
        while pos < len(audio) and not self._stop.is_set():
            due = int((time.perf_counter() - t0) * SR)
            if due - pos >= block:
                n = min(due - pos, len(audio) - pos)
                self.on_audio(audio[pos : pos + n])
                pos += n
            time.sleep(0.01)
        if not self._stop.is_set():
            self.on_status(f"样例回放结束：{Path(self.path).name}（换回其他声音来源即可正式使用）", True)
        if self.done:
            self.done()


def source_spec(key: str) -> SourceSpec:
    if key in config.APP_SOURCES:
        label, names = config.APP_SOURCES[key]
        return SourceSpec("process", process_names=names, label=label)
    if key.startswith("device:"):
        did = key.split(":", 1)[1]
        return SourceSpec("device", device_id=None if did == "default" else did, label="播放设备")
    raise ValueError(key)


# ---------------------------------------------------------------- records
class Recorder:
    def __init__(self, meta: dict):
        self.meta = meta
        self.dir: Path | None = None

    def add(self, cap: dict):
        if self.dir is None:
            self.dir = config.RECORD_DIR / datetime.now().strftime("%Y-%m-%d_%H%M%S")
            self.dir.mkdir(parents=True, exist_ok=True)
            head = (f"# Teams 会议字幕 {datetime.now():%Y-%m-%d %H:%M}\n\n"
                    f"- 声音来源：{self.meta.get('source', '')}\n- 识别：faster-whisper large-v3-turbo；翻译：{self.meta.get('translator', '')}\n"
                    f"- 机器实时翻译，可能有识别或翻译错误；重要内容请以德语原文/会议录音为准。\n\n")
            (self.dir / "字幕.md").write_text(head, encoding="utf-8")
        stamp = datetime.fromtimestamp(cap["wall_time"]).strftime("%H:%M:%S")
        with open(self.dir / "字幕.md", "a", encoding="utf-8") as f:
            f.write(f"**{stamp}**　{cap['zh']}\n\n> {cap['de']}\n\n")
        with open(self.dir / "captions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(cap, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- engine
@dataclasses.dataclass
class Job:
    kind: str  # provisional | ending (tentative pause) | final
    seq: int
    start: int
    end: int
    speech_end: int | None = None
    generation: int = 0
    gap: int | None = None  # first quiet sample, unique within a VAD sequence


def sentence_ended(text: str) -> bool:
    text = text.rstrip().rstrip("\"'»“”’)]").rstrip()
    if text.endswith(("?", "!")):
        return True
    return (text.endswith(".") and not text.endswith(("..", "…."))
            and not re.search(r"(?:\b\w|\d|\b(?:dr|prof|bzw|usw|ca|nr|inkl|zzgl|ggf|dipl|str|hr|fr"
                              r"|und|oder|aber|weil|dass|wenn|obwohl|mit|für|zum|zur))\.$", text, re.I))


def choose_cut(words: list[tuple[float, float, str]], total: float, keep_tail: float = 2.0, min_head: float = 3.0):
    """Index of the last word of a committable head and the cut time, or None."""
    cands = [i for i in range(len(words) - 1) if min_head <= words[i][1] <= total - keep_tail]
    if not cands:
        return None
    pick = None
    for pattern in (r"[.!?…][\"»”']?$", r"[,;:]$"):
        hits = [i for i in cands if re.search(pattern, words[i][2].strip())]
        if hits:
            pick = hits[-1]
            break
    if pick is None:
        pick = max(cands, key=lambda i: (words[i + 1][0] - words[i][1], i))
    return pick, (words[pick][1] + words[pick + 1][0]) / 2


class CaptionEngine:
    """Owns models, threads and the GPU lock. ``emit(event: dict)`` is called from worker threads."""

    def __init__(self, cfg: dict, emit):
        self.cfg = cfg
        self.emit = emit
        self.state = "idle"
        self.model = None
        self.translator = None
        self.capture = None
        self.vad: CausalVAD | None = None
        self.ring: AudioRing | None = None
        self.gpu_lock = None
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self.generation = 0
        self.paused = False
        self.cond = threading.Condition()
        self.worker: threading.Thread | None = None
        self.draft_pool: ThreadPoolExecutor | None = None
        self.recorder: Recorder | None = None
        self.replay: str | None = None
        self.replay_done = None
        self.lat = collections.deque(maxlen=30)       # pause-final lag (speech end -> final caption)
        self.prov_lat = collections.deque(maxlen=30)  # provisional lag (newest audio -> caption)
        self.count = 0
        self._terms = Glossary(cfg.get("terms", ""))
        self.prompt_key, self.prompt = None, None
        self.candidate = None
        self.last_attempt = None

    # ---- lifecycle -------------------------------------------------------------------
    def _status(self, state: str, text: str):
        self.state = state
        log.info("[%s] %s", state, text)
        self.emit({"type": "status", "state": state, "text": text})

    def start_async(self):
        if self.state not in ("idle", "error"):
            return
        self._cancel.clear()
        self.state = "loading"
        threading.Thread(target=self._load_and_run, name="engine-load", daemon=True).start()

    def _check_cancel(self):
        if self._cancel.is_set():
            raise InterruptedError("已取消")

    def _load_and_run(self):
        try:
            from filelock import FileLock, Timeout

            self._status("loading", "检查 GPU…")
            if self.cfg.get("gpu_lock"):
                # Acquired by engine-load and released by the shutdown thread.
                self.gpu_lock = FileLock(str(self.cfg["gpu_lock"]), timeout=0, thread_local=False)
                try:
                    self.gpu_lock.acquire()
                except Timeout:
                    self.gpu_lock = None
                    raise RuntimeError("GPU 正被其他转写任务占用（共享 GPU 锁）。请停止该任务或等它结束后再开始字幕。")
            need = 21000 if self.cfg["translator"] == "qwen" else 10000
            used, total = gpu_memory()
            if total and total - used < need and not self._translator_already_running():
                self.emit({"type": "warning", "text": f"显存可能不足：已用 {used/1024:.1f} / {total/1024:.1f} GB，本模式约需 {need/1024:.0f} GB。若加载失败，请关闭其他占用显卡的程序或改用省显存模式。"})
            self._check_cancel()

            self._status("loading", "加载语音识别模型 large-v3-turbo…")
            from faster_whisper import WhisperModel

            model_dir = Path(self.cfg["asr_model_dir"])
            if not model_dir.is_absolute():
                model_dir = config.REPO / model_dir
            if not (model_dir / "model.bin").is_file():
                raise FileNotFoundError(f"找不到语音识别模型：{model_dir}（运行 setup.ps1 下载，或在 config.json 设置 asr_model_dir）")
            self.model = WhisperModel(str(model_dir), device="cuda", compute_type="float16", cpu_threads=8, num_workers=1, local_files_only=True)
            rng = np.random.default_rng(0)
            list(self.model.transcribe((rng.standard_normal(SR * 3) * 0.01).astype(np.float32), language="de", beam_size=self.cfg["beam_size"], temperature=0, vad_filter=False)[0])
            self._check_cancel()

            from .translate import TRANSLATORS

            self.translator = TRANSLATORS[self.cfg["translator"]](self.cfg, log.info)
            self.translator.start(lambda text: self._status("loading", text))
            self._check_cancel()
            self._status("loading", "预热翻译模型…")
            greeting = "Hallo zusammen, können Sie mich hören?"
            topic = self.cfg["topic"]
            self.translator.translate(greeting, "", topic, self.glossary.translation_hints(greeting, topic))
            self._check_cancel()

            self._start_pipeline()
        except Exception as exc:
            log.exception("start failed")
            self._unload()
            if isinstance(exc, InterruptedError):
                self._status("idle", "已取消加载")
            else:
                self._status("error", str(exc))

    def _translator_already_running(self) -> bool:
        if self.cfg["translator"] == "qwen":
            from .translate import LLMTranslator

            return LLMTranslator(self.cfg, log.info).ready()
        return False

    def _start_pipeline(self):
        self.ring = AudioRing()
        self._stop.clear()
        self.vad = CausalVAD(self.ring, self.cfg["vad_final_silence_ms"], self._poke,
                             self.cfg.get("vad_punctuated_silence_ms", 384))
        self.vad.start()
        self.committed: dict[int, int] = {}
        self.parts: dict[int, int] = {}
        self.live_id: str | None = None
        self.mt_key = None
        self.mt_text = ""
        self.final_asr = None
        self.candidate = self.last_attempt = None
        self.draft_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="draft-mt")
        self.context_hist: collections.deque = collections.deque(maxlen=6)
        self.tick_seq, self.next_tick = -1, 0
        self.last_level_emit = 0.0
        self.level_acc = []
        self.worker = threading.Thread(target=self._work, name="caption-worker", daemon=True)
        self.worker.start()
        self._open_source()
        self.paused = False
        self._status("running", "字幕运行中")

    def _open_source(self):
        replay = self.replay or (self.cfg["source"][7:] if self.cfg["source"].startswith("replay:") else None)
        if replay:
            self.capture = FileSource(replay, self._on_audio, self._on_source, done=self.replay_done)
        else:
            self.capture = LoopbackCapture(source_spec(self.cfg["source"]), self._on_audio, self._on_source)
        self.recorder = Recorder({"source": "", "translator": self.translator.label if self.translator else ""})
        self.capture.start()

    def _on_source(self, text: str, ok: bool):
        if self.recorder:
            self.recorder.meta["source"] = getattr(self.capture, "active_label", text) or text
        self.emit({"type": "source", "text": text, "ok": ok})

    def set_source(self, key: str):
        self.cfg["source"] = key
        if self.state == "running" and not self.paused and not self.replay:
            old = self.capture
            self.capture = None
            if old:
                old.stop()
            self.generation += 1
            self.vad.reset(self.ring.total)
            self._poke()
            self._open_source()

    def pause(self):
        if self.state != "running" or self.paused:
            return
        self.paused = True
        self.generation += 1
        if self.capture:
            self.capture.stop()
            self.capture = None
        self.vad.reset(self.ring.total)
        self._poke()
        self._status("running", "已暂停（模型仍在显存中，可随时继续）")
        self.emit({"type": "paused", "paused": True})

    def resume(self):
        if self.state != "running" or not self.paused:
            return
        self.vad.reset(self.ring.total)
        self.paused = False
        self._open_source()
        self._status("running", "字幕运行中")
        self.emit({"type": "paused", "paused": False})

    def shutdown(self):
        """Stop everything and free GPU memory (blocking; call from a worker thread)."""
        self._cancel.set()
        if self.state == "loading":
            for _ in range(600):  # loading thread cleans up itself
                if self.state != "loading":
                    break
                time.sleep(0.1)
        if self.state in ("running", "stopping"):
            self._status("stopping", "正在停止并释放显存…")
            self._unload()
            self._status("idle", "已停止，显存已释放")

    def _unload(self):
        self._stop.set()
        self.generation += 1
        self._poke()
        if self.capture:
            self.capture.stop()
            self.capture = None
        if self.vad:
            self.vad.stop()
            self.vad = None
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join()
        self.worker = None
        if self.draft_pool is not None:
            self.draft_pool.shutdown(wait=True)
            self.draft_pool = None
        if self.translator:
            try:
                if self.cfg.get("stop_owned_server_on_exit", True) or self.translator.name != "qwen":
                    self.translator.stop()
            except Exception:
                log.exception("translator stop failed")
            self.translator = None
        self.model = None
        self.prompt_key = self.prompt = None
        gc.collect()
        if self.gpu_lock:
            try:
                self.gpu_lock.release()
            except Exception:
                pass
            self.gpu_lock = None

    # ---- audio ----------------------------------------------------------------------
    def _on_audio(self, x: np.ndarray):
        if self.paused or self.ring is None:
            return
        self.ring.append(x)
        self.level_acc.append(float(np.sqrt(np.mean(x * x))) if len(x) else 0.0)
        now = time.perf_counter()
        if now - self.last_level_emit > 0.08:
            self.last_level_emit = now
            rms = max(self.level_acc) if self.level_acc else 0.0
            self.level_acc = []
            seq, active = self.vad.snapshot() if self.vad else (0, None)
            self.emit({"type": "level", "rms": rms, "speaking": active is not None})

    def _poke(self):
        with self.cond:
            self.cond.notify_all()

    # ---- worker ---------------------------------------------------------------------
    def _next_job(self) -> Job | None:
        hop = int(self.cfg["update_seconds"] * SR)
        with self.cond:
            if self._stop.is_set():
                return None
            vad = self.vad
            if vad is None or self.paused:
                self.cond.wait(1.0)
                return None
            with vad.lock:
                if vad.finals:
                    f = vad.finals.popleft()
                    return Job("final", f["seq"], f["start"], f["end"], f["speech_end"], self.generation, f.get("gap"))
                seq, active, silence, pos = vad.seq, vad.active, vad.silence, vad.pos
            if active is None:
                self.cond.wait(1.0)  # VAD wakes us on speech onset. Timeout services idle release.
                return None
            start = max(active, self.committed.get(seq, active), self.ring.base)
            if seq != self.tick_seq:
                first = min(hop, int(self.cfg.get("first_update_seconds", 1.4) * SR))
                self.tick_seq, self.next_tick = seq, start + first
            now = self.ring.total
            window_end = start + int(self.cfg["window_seconds"] * SR)
            end = min(now, window_end)
            if silence is not None:
                attempt = (self.generation, seq, start, silence)
                if self.last_attempt != attempt and start < pos <= window_end:
                    self.last_attempt = attempt
                    return Job("ending", seq, start, pos, generation=self.generation, gap=silence)
                self.cond.wait(1.0)  # VAD wakes us on resumption or confirmation.
                return None
            target = max(self.next_tick, start + SR // 2)
            if now < target:
                self.cond.wait((target - now) / SR)
                return None
            # Advance the first look only. Keep later updates and long-sentence
            # splits on their regular cadence, without an extra decode at 2 s.
            self.next_tick = max(now + hop, active + 2 * hop)
            # If inference fell behind, split from the oldest uncommitted audio.
            # Sliding the *start* forward can silently discard the first words.
            return Job("provisional", seq, start, end, generation=self.generation)

    def _current(self, job: Job) -> bool:
        return not self.paused and not self._stop.is_set() and job.generation == self.generation

    def _work(self):
        while not self._stop.is_set():
            job = self._next_job()
            if job is None:
                self._idle_check()
                continue
            try:
                if job.kind == "final":
                    self._final(job)
                else:
                    self._provisional(job)
            except CancelledError:
                continue
            except Exception as exc:
                log.exception("caption job failed")
                self.emit({"type": "warning", "text": f"本句处理失败：{exc}"})
                time.sleep(0.5)

    def _idle_check(self):
        minutes = self.cfg.get("idle_release_minutes") or 0
        if minutes and self.vad and not self.paused and time.perf_counter() - self.vad.last_speech_wall > minutes * 60:
            self.vad.last_speech_wall = time.perf_counter()
            self.emit({"type": "idle_release", "minutes": minutes})

    def _cid(self, seq: int) -> str:
        return f"{seq}.{self.parts.get(seq, 0)}"

    @property
    def glossary(self):
        raw = self.cfg.get("terms", "")
        glossary = self._terms
        if raw != glossary.raw:
            glossary = self._terms = Glossary(raw)
        return glossary

    def _recognition_prompt(self):
        glossary = self.glossary
        if not glossary.entries:
            return None
        topic = self.cfg.get("topic", "")
        recent = self.context_hist[-1] if self.context_hist else ""
        tokenizer = self.model.hf_tokenizer
        key = (glossary, tokenizer, topic, recent)
        if key != self.prompt_key:
            self.prompt = glossary.recognition_prompt(topic, recent, tokenizer)
            self.prompt_key = key
        return self.prompt

    def _asr(self, audio: np.ndarray, *, word_timestamps: bool, beam_size: int, prompt: str | None, cancelled=None):
        t = time.perf_counter()
        segs, _ = self.model.transcribe(audio, language="de", beam_size=beam_size, temperature=0, condition_on_previous_text=False,
                                        word_timestamps=word_timestamps, vad_filter=True, vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 150},
                                        initial_prompt=prompt)
        kept, words = [], []
        if cancelled and cancelled():  # inference is lazy; skip a gap lost during audio preprocessing
            raise CancelledError()
        for s in segs:
            text = s.text.strip()
            if not text or HALLUCINATION.search(text) or s.compression_ratio > 2.4 or (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0):
                continue
            kept.append(text)
            words += [(w.start, w.end, w.word) for w in (s.words or [])]
        return " ".join(kept).strip(), words, time.perf_counter() - t

    def _translate(self, job: Job, cid: str, source: str, start: int, end: int, superseded=None) -> tuple[str, float]:
        topic = self.cfg["topic"]
        key = (source, " ".join(self.context_hist), topic, self.glossary.translation_hints(source, topic), self.cfg["context_chars"])
        if self.mt_key == key:
            return self.mt_text, 0.0

        def cancelled():
            return (not self._current(job) or (superseded is not None and superseded.is_set())
                    or (job.kind == "ending" and not self._gap_current(job)))

        def preview(text: str):
            if not cancelled():
                self._emit_caption(cid, "provisional", source, text, start, end, streaming=True)

        try:
            r = self.translator.translate(*key, on_partial=preview, cancelled=cancelled)
        except CancelledError:
            raise
        except Exception:
            if self.live_id == cid and self._current(job):
                self.live_id = None
                self.emit({"type": "drop", "id": cid})
            raise
        if cancelled():
            raise CancelledError()
        if r["translation"]:
            self.mt_key, self.mt_text = key, r["translation"]
        return r["translation"], r["mt_s"]

    def _gap_current(self, job: Job):
        vad = self.vad
        if job.gap is None or vad is None or not self._current(job):
            return False
        with vad.lock:
            return ((vad.seq == job.seq and vad.active is not None and vad.silence == job.gap)
                    or any(f["seq"] == job.seq and f.get("gap") == job.gap for f in vad.finals))

    def _endpoint_asr(self, job: Job, start: int):
        prompt = self._recognition_prompt()
        beam = self.cfg["beam_size"]
        key = (job.generation, job.seq, start, job.gap, prompt, beam)
        if job.gap is not None and self.candidate and self.candidate[0] == key:
            return self.candidate[1], 0.0
        full_key = (key, job.end)
        if self.final_asr and self.final_asr[0] == full_key:
            return self.final_asr[1], 0.0
        vad = self.vad
        cancelled = (lambda: not self._gap_current(job)) if job.kind == "ending" else None
        de, _, seconds = self._asr(self.ring.get(start, job.end), word_timestamps=False, beam_size=beam,
                                   prompt=prompt, cancelled=cancelled)
        if job.kind == "ending":
            if not self._gap_current(job):
                raise CancelledError()
            if de:
                self.candidate = (key, de)
                vad.note_transcript(job.seq, job.end, de)
        else:
            self.final_asr = (full_key, de)
        return de, seconds

    def _translate_draft(self, job: Job, cid: str, de: str, start: int):
        # At most one MT request in flight. The ASR worker can use a confirmed
        # pause immediately, even if the last draft's translation is still running.
        superseded = threading.Event()
        pending = self.draft_pool.submit(self._translate, job, cid, de, start, job.end, superseded)
        pending.add_done_callback(lambda _: self._poke())
        checked_gap = job.gap
        try:
            while True:
                with self.cond:
                    vad = self.vad
                    if vad is None or pending.done() or not self._current(job):
                        break
                    with vad.lock:
                        if vad.finals and vad.finals[0]["seq"] == job.seq:
                            f = vad.finals[0]
                            check = Job("final", job.seq, start, f["end"], f["speech_end"], job.generation, f.get("gap"))
                        elif (vad.seq == job.seq and vad.active is not None and vad.silence is not None
                              and vad.silence != checked_gap and start < vad.pos <= start + int(self.cfg["window_seconds"] * SR)):
                            check = Job("ending", job.seq, start, vad.pos, generation=job.generation, gap=vad.silence)
                            checked_gap = vad.silence
                        else:
                            check = None
                    if check is None:
                        self.cond.wait()
                        continue
                try:
                    endpoint_de, _ = self._endpoint_asr(check, start)
                except CancelledError:  # speech resumed while this gap was being decoded
                    continue
                if endpoint_de and endpoint_de != de:
                    superseded.set()
                    break
                if check.kind == "final":
                    break
        finally:
            # Never queue another translation behind a stale request, including on errors.
            zh, mt_s = pending.result()
        if superseded.is_set():
            raise CancelledError()
        return zh, mt_s

    def _emit_caption(self, cid: str, state: str, de: str, zh: str | None, job_start: int, end: int, **extra):
        cap = {"type": "caption", "id": cid, "state": state, "de": de, "zh": zh, "wall_time": time.time() - (time.perf_counter() - self.ring.wall_of(job_start)), **extra}
        if zh:
            self.live_id = cid if state == "provisional" else None
        self.emit(cap)
        return cap

    def _provisional(self, job: Job):
        window = int(self.cfg["window_seconds"] * SR)
        a = job.start
        dur = (job.end - a) / SR
        split_after = self.cfg["window_seconds"] - self.cfg["update_seconds"]
        ending = job.kind == "ending"
        if ending:
            de, asr_s = self._endpoint_asr(job, a)
            words = []
        else:
            can_split = dur >= split_after
            beam = self.cfg["beam_size"] if can_split else 1
            de, words, asr_s = self._asr(self.ring.get(a, job.end), word_timestamps=can_split,
                                         beam_size=beam, prompt=self._recognition_prompt())
        if not self._current(job):
            return
        if not de:
            if not ending and job.end - a >= window:
                # A full undecodable window would otherwise be retried forever.
                # Keep overlap so a word starting at its right edge can be recovered.
                self.committed[job.seq] = job.end - min(SR, window // 2)
            return
        vad = self.vad
        if vad is None:
            return
        if not ending:
            vad.note_transcript(job.seq, job.end, de)
            with vad.lock:
                if vad.seq != job.seq or vad.active is None:
                    return
                if vad.silence is not None and not can_split:
                    return
        if not ending and dur >= split_after:
            cut = choose_cut(words, dur)
            if cut is None and dur >= self.cfg["window_seconds"] - 0.5:
                cut = (len(words) - 1, words[-1][1]) if words else None
            if cut is not None or job.end - a >= window:
                head, cut_end = de, job.end
                if cut is not None:
                    i, t = cut
                    if int(t * SR) > 0:
                        head = "".join(w[2] for w in words[: i + 1]).strip()
                        cut_end = min(job.end, a + int(t * SR))
                cid = self._cid(job.seq)
                if head and self._finalize(job, cid, head, a, cut_end, asr_s, reason="split"):
                    self.committed[job.seq] = cut_end
                    self.parts[job.seq] = self.parts.get(job.seq, 0) + 1
                    self.next_tick = 0  # recognise the remainder right away
                return
        cid = self._cid(job.seq)
        if self.live_id != cid:
            self._emit_caption(cid, "provisional", de, None, a, job.end)
        zh, mt_s = self._translate_draft(job, cid, de, a)
        if not self._current(job) or (ending and not self._gap_current(job)):
            return
        lag = time.perf_counter() - self.ring.wall_of(job.end)
        self.prov_lat.append(lag)
        self._emit_caption(cid, "provisional", de, zh, a, job.end, asr_s=round(asr_s, 3), mt_s=round(mt_s, 3), lag_s=round(lag, 2))

    def _final(self, job: Job):
        cid = self._cid(job.seq)
        start = max(job.start, self.committed.pop(job.seq, job.start))
        self.parts.pop(job.seq, None)
        if job.end - start < int(0.3 * SR):
            self.emit({"type": "drop", "id": cid})
            return
        de, asr_s = self._endpoint_asr(job, start)
        if not self._current(job):
            return
        if not de:
            self.emit({"type": "drop", "id": cid})
            return
        self._finalize(job, cid, de, start, job.end, asr_s, speech_end=job.speech_end)

    def _finalize(self, job: Job, cid: str, de: str, start: int, end: int, asr_s: float, speech_end: int | None = None, reason: str = "pause") -> bool:
        zh, mt_s = self._translate(job, cid, de, start, end)
        if not self._current(job):
            return False
        ref = speech_end if speech_end is not None else end
        lag = time.perf_counter() - self.ring.wall_of(ref)
        cap = self._emit_caption(cid, "final", de, zh, start, end, asr_s=round(asr_s, 3), mt_s=round(mt_s, 3), lag_s=round(lag, 2), reason=reason,
                                 audio_s=round((end - start) / SR, 2))
        self.context_hist.append(de)
        if reason == "pause":
            self.lat.append(lag)
        self.count += 1
        if self.recorder:
            try:
                self.recorder.add(cap)
            except Exception:
                log.exception("record failed")
        self.emit({"type": "metrics", "count": self.count, "lag_median": float(np.median(self.lat)) if self.lat else None,
                   "prov_lag_median": float(np.median(self.prov_lat)) if self.prov_lat else None, "asr_s": asr_s, "mt_s": mt_s,
                   "record_dir": str(self.recorder.dir) if self.recorder and self.recorder.dir else ""})
        return True


def gpu_memory() -> tuple[int, int]:
    """(used MiB, total MiB) of GPU 0 or (0, 0)."""
    try:
        out = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits", "-i", "0"],
                                      text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        used, total = (int(x) for x in out.strip().split(","))
        return used, total
    except Exception:
        return 0, 0


# ---------------------------------------------------------------- headless test
def _cli():
    import argparse

    p = argparse.ArgumentParser(description="Headless engine run (file replay or live loopback) for testing.")
    p.add_argument("--replay", help="16 kHz mono WAV, fed at real-time pace")
    p.add_argument("--source", default=None, help="auto | edge | chrome | device:default")
    p.add_argument("--translator", default=None, choices=["qwen", "hymt7b"])
    p.add_argument("--seconds", type=float, default=0, help="stop live capture after N seconds")
    p.add_argument("--out", help="write events as JSON lines")
    p.add_argument("--topic", default=None)
    p.add_argument("--record-dir", default=None, help="where to write 字幕.md (default: 会议记录)")
    args = p.parse_args()

    cfg = config.load()
    config.prepare_environment(cfg)
    if args.record_dir:
        config.RECORD_DIR = Path(args.record_dir)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.translator:
        cfg["translator"] = args.translator
    if args.source:
        cfg["source"] = args.source
    if args.topic is not None:
        cfg["topic"] = args.topic
    out = open(args.out, "w", encoding="utf-8") if args.out else None
    finished = threading.Event()
    t0 = time.perf_counter()

    running = threading.Event()

    def emit(ev):
        ev = dict(ev, t=round(time.perf_counter() - t0, 3))
        if ev["type"] == "status" and ev["state"] == "running":
            running.set()
        if ev["type"] != "level":
            print(json.dumps(ev, ensure_ascii=False), flush=True)
        if out and ev["type"] != "level":
            out.write(json.dumps(ev, ensure_ascii=False) + "\n")
            out.flush()
        if ev["type"] == "status" and ev["state"] == "error":
            finished.set()

    eng = CaptionEngine(cfg, emit)
    if args.replay:
        eng.replay = args.replay
        eng.replay_done = lambda: threading.Timer(6, finished.set).start()
    eng.start_async()
    try:
        if args.seconds and not args.replay:
            while not (running.is_set() or finished.is_set()):
                time.sleep(0.2)
            finished.wait(args.seconds)
        else:
            finished.wait()
    except KeyboardInterrupt:
        pass
    eng.shutdown()
    if out:
        out.close()


if __name__ == "__main__":
    _cli()
