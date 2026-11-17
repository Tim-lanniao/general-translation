"""Environment check (no GPU model is loaded). Run: python -m teams_caption.selfcheck"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_caption import config  # noqa: E402

cfg = config.load()
config.prepare_environment(cfg)
ok = True


def check(name, cond, detail="", required=True):
    global ok
    tag = "[ok]   " if cond else ("[FAIL] " if required else "[warn] ")
    print(tag + name + (f" - {detail}" if detail else ""))
    if required:
        ok &= bool(cond)


print(f"[info] data directory: {config.DATA_DIR}")
try:
    import PySide6  # noqa: F401
    import ctranslate2
    import faster_whisper
    import scipy  # noqa: F401
    from faster_whisper.vad import get_vad_model

    check("Python packages", True, f"ctranslate2 {ctranslate2.__version__}, faster-whisper {faster_whisper.__version__}")
    check("CUDA visible to CTranslate2", ctranslate2.get_cuda_device_count() > 0)
    get_vad_model()
    check("Silero VAD", True)
except Exception as exc:  # noqa: BLE001
    check("Python packages", False, repr(exc))

model = Path(cfg["asr_model_dir"])
model = model if model.is_absolute() else config.REPO / model
check("ASR model large-v3-turbo", (model / "model.bin").is_file(), str(model))
dlls = config.cuda_dll_dirs(cfg)
check("CUDA DLLs (cuBLAS 12 / cuDNN 9)", any((d / "cublas64_12.dll").is_file() for d in dlls) or not dlls,
      "; ".join(map(str, dlls)) or "not configured, relying on PATH")
check("LLM controller (optional)", Path(cfg["llm_controller"]).is_file() if cfg["llm_controller"] else False,
      cfg["llm_controller"] or f"not set: start {cfg['llm_url']} yourself", required=False)
check("Hy-MT2-7B mode (optional)", bool(cfg["llama_server"]) and Path(cfg["llama_server"]).is_file() and bool(cfg["hymt_model"]) and Path(cfg["hymt_model"]).is_file(),
      "llama_server / hymt_model", required=False)

from teams_caption import capture  # noqa: E402

devices = capture.list_render_devices()
check("Playback devices", bool(devices), "; ".join(d.name + (" (default)" if d.default else "") for d in devices))
pid = capture.find_root_process(list(config.APP_SOURCES["auto"][1]))
print(f"[info] Teams process: {pid if pid else 'not running (auto mode falls back to the default playback device)'}")
sys.exit(0 if ok else 1)
