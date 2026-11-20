"""Where the desk finds the other components and keeps its own settings.

The data root is the one the gateway and the audio app use: ``%TRANSLATION_DATA%``, else
``<repo>/data``. The desk keeps its settings in ``<data root>/text``.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ROOT = Path(os.environ.get("TRANSLATION_DATA") or REPO / "data").resolve()
DATA_DIR = ROOT / "text"
SETTINGS_PATH = DATA_DIR / "desk.json"
LOG_DIR = DATA_DIR / "logs"
GATEWAY_LAUNCHER = REPO / "gateway" / "translation-gateway.cmd"
AUDIO_DIR = REPO / "audio"

DEFAULTS = {"source": "auto", "target": "zh", "geometry": None}


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(saved, dict):
            settings.update(saved)
    except (FileNotFoundError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=DATA_DIR, delete=False, mode="w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    try:
        os.replace(f.name, SETTINGS_PATH)
    finally:
        Path(f.name).unlink(missing_ok=True)
