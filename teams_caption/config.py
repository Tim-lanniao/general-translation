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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TEAMS_CAPTION_DATA") or REPO / "data")
RECORD_DIR = DATA_DIR / "会议记录"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"
ICON_PATH = Path(__file__).resolve().parent / "icon.ico"

# Recognised sources. "auto" = Teams program audio, default playback device while Teams is not running.
APP_SOURCES = {
    "auto": ("Microsoft Teams", ("ms-teams.exe", "Teams.exe")),
    "edge": ("Edge 浏览器（网页版 Teams）", ("msedge.exe",)),
    "chrome": ("Chrome 浏览器（网页版 Teams）", ("chrome.exe",)),
}

DEFAULTS = {
    # ---- user settings (changed from the UI)
    "source": "auto",            # auto | edge | chrome | device:<id> | device:default | replay:<wav>
    "translator": "qwen",        # qwen = local OpenAI-compatible LLM (recommended) | hymt7b = Hy-MT2-7B via llama.cpp
    "topic": "",                 # meeting subject, used in the translation prompt
    "terms": "",                 # names / jargon, comma separated
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
    "stop_owned_server_on_exit": True,
    "idle_release_minutes": 45,
    # ---- pipeline (values from the author's real-time replay evaluation)
    "window_seconds": 12,
    "update_seconds": 2,
    "first_update_seconds": 1.4, # first draft only; subsequent updates use update_seconds
    "vad_final_silence_ms": 600,
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
    "llama_server": "",          # llama-server.exe for the hymt7b mode
    "hymt_model": "",            # HY-MT2-7B-Q6_K.gguf
    "hymt_port": 18082,
    "sample_wav": "",            # optional 16 kHz mono German WAV offered as a test source
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
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
