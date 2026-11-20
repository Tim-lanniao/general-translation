"""Paths and persisted settings.

Everything machine-specific (model paths, CUDA DLLs, the local LLM server, a shared
GPU lock) lives in ``config.json`` inside the data directory, which is never part of
the repository. See ``config.example.json``.

Data directory: ``%TEAMS_CAPTION_DATA%`` if set, otherwise ``<repo>/data``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from filelock import FileLock

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TEAMS_CAPTION_DATA") or REPO / "data")
RECORD_DIR = DATA_DIR / "会议记录"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"
ICON_PATH = Path(__file__).resolve().parent / "icon.ico"

# Recognised sources. "auto" = Teams program audio, default playback device while Teams is not running.
APP_SOURCES = {
    "auto": ("Microsoft Teams", ("ms-teams.exe", "Teams.exe")),
    "edge": ("Edge 浏览器", ("msedge.exe",)),
    "chrome": ("Chrome 浏览器", ("chrome.exe",)),
}

DEFAULTS = {
    # ---- user settings (changed from the UI)
    "source": "system",          # all desktop audio, independent of playback endpoint
    "audio_source_version": 2,
    "asr_language": "de",
    "translator": "qwen",
    "topic": "",                 # meeting subject, used in the translation prompt
    "terms": "",                 # German=Chinese lines, optional [scene] sections
    "auto_start": True,
    "show_german": True,
    "font_px": 28,
    "overlay_opacity": 0.82,
    "overlay_geometry": None,    # [x, y, w]
    "click_through": False,
    "hide_from_capture": True,   # exclude the subtitle bar from screen sharing / recording
    "main_lines": 3,
    "auto_collapse_settings": True,
    "history_font_px": 16,
    "idle_release_minutes": 45,
    "gateway_port": 18083,
    "gateway_idle_seconds": 300,
    "gateway_lan": False,        # also listen on the LAN; remote clients need a device token
    "gateway_networks": [],      # remote client networks in CIDR; empty = private LAN ranges
    "gateway_cache": True,       # reuse finished text translations (captions are never cached)
    "gateway_cache_entries": 20000,
    "llm_parallel": 1,           # generations the inference server runs at once (Strata parallel requests)
    # ---- pipeline (values from the author's real-time replay evaluation)
    "window_seconds": 12,
    "update_seconds": 2,
    "first_update_seconds": 1.4, # first draft only; subsequent updates use update_seconds
    "vad_final_silence_ms": 600,
    "vad_punctuated_silence_ms": 384,
    "beam_size": 5,
    "context_chars": 160,
    # ---- machine-specific paths (set in config.json; see config.example.json)
    "asr_model_dir": str(REPO / "models" / "faster-whisper-large-v3-turbo"),
    "cuda_dll_dir": "",          # folder with cublas64_12.dll / cudnn64_9.dll; empty = pip nvidia-* packages or PATH
    "cache_dir": "",             # empty = <data>/cache
    "gpu_lock": "",              # optional lock file shared with other GPU jobs; empty = none
    "llm_url": "http://127.0.0.1:18081",
    "llm_model": "qwen3.8-flash-next-iq3_s",
    "llm_label": "Qwen3.8-Flash-Next",
    "llm_controller": "",        # optional .ps1 accepting start | stop | status; empty = server must already run
    "sample_wav": "",            # optional 16 kHz mono German WAV offered as a test source
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        cfg.update(saved)
        if saved.get("audio_source_version", 1) < 2 and cfg["source"] in ("auto", "device:default"):
            cfg["source"] = "system"
        cfg["audio_source_version"] = 2
        cfg["translator"] = "qwen"
        for retired in ("llama_server", "hymt_model", "hymt_port", "stop_owned_server_on_exit"):
            cfg.pop(retired, None)
    except FileNotFoundError:
        pass
    except Exception:
        # A broken file must not block a meeting; keep a copy and use defaults.
        CONFIG_PATH.replace(CONFIG_PATH.with_suffix(".broken.json"))
    return cfg


def save(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


LANGUAGES = {"auto": "自动检测", "de": "德语", "en": "英语", "fr": "法语", "ja": "日语", "es": "西班牙语"}


def shared_terms_path() -> Path:
    return DATA_DIR / "gateway-terms.txt"


def read_shared_terms(cfg: dict) -> str:
    try:
        return shared_terms_path().read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return cfg.get("terms", "")  # existing installations, until the first migration


def save_shared_terms(raw: str, *, initialize=False) -> None:
    """Publish a complete glossary; initialization never overwrites another editor."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with FileLock(str(DATA_DIR / "gateway-terms.lock"), timeout=5):
        if initialize and shared_terms_path().exists():
            return
        with tempfile.NamedTemporaryFile(dir=DATA_DIR, delete=False, mode="w", encoding="utf-8") as f:
            f.write(raw)
        try:
            os.replace(f.name, shared_terms_path())
        finally:
            Path(f.name).unlink(missing_ok=True)


def shared_terms_revision():
    try:
        info = shared_terms_path().stat()
        return info.st_mtime_ns, info.st_size, info.st_ino
    except FileNotFoundError:
        return None


def cache_dir(cfg: dict) -> Path:
    return Path(cfg.get("cache_dir") or DATA_DIR / "cache")


def cuda_dll_dirs(cfg: dict) -> list[Path]:
    if cfg.get("cuda_dll_dir"):
        return [Path(cfg["cuda_dll_dir"])]
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    return [p for p in (site / "cublas" / "bin", site / "cudnn" / "bin", site / "cuda_runtime" / "bin") if p.is_dir()]


def prepare_environment(cfg: dict) -> None:
    """Process-local environment (offline models, caches, CUDA DLL search path)."""
    os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    cache = cache_dir(cfg)
    os.environ["HF_HOME"] = str(cache / "huggingface")
    os.environ["CUDA_CACHE_PATH"] = str(cache / "cuda")
    tmp = cache / "tmp"
    for p in (tmp, DATA_DIR, RECORD_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(tmp)
    for d in cuda_dll_dirs(cfg):
        if d.is_dir():
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            os.add_dll_directory(str(d))
