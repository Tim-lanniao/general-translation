"""Gateway settings and data directory.

The gateway owns the model settings, the shared glossary, device tokens and the
translation cache. Its data live in ``<data root>/gateway``; the audio caption app keeps
its own settings next to it in ``<data root>/audio``.

Data root: ``%TRANSLATION_DATA%``, else ``<repo>/data``, always as an absolute path so that
a gateway started from another working directory finds the same files.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from filelock import FileLock

REPO = Path(__file__).resolve().parents[2]
COMPONENT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("TRANSLATION_DATA") or REPO / "data").resolve()
DATA_DIR = ROOT / "gateway"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_DIR = DATA_DIR / "logs"

DEFAULTS = {
    "gateway_port": 18083,
    "gateway_idle_seconds": 300,
    "gateway_lan": False,        # also listen on the LAN; remote clients need a device token
    "gateway_networks": [],      # remote client networks in CIDR; empty = private LAN ranges
    "gateway_cache": True,       # reuse finished text translations (captions are never cached)
    "gateway_cache_entries": 20000,
    "topic": "",                 # default scene for text requests that send none
    "llm_url": "http://127.0.0.1:18081",
    "llm_model": "qwen3.8-flash-next-iq3_s",
    "llm_label": "Qwen3.8-Flash-Next",
    "llm_controller": "",        # optional .ps1 accepting start | stop | status; empty = server must already run
    "llm_parallel": 1,           # generations the inference server runs at once (Strata parallel requests)
    "gpu_lock": "",              # optional lock file shared with other GPU jobs; empty = none
    "cache_dir": "",             # empty = <data root>/gateway/cache
}

# Language names for prompts; clients send ISO codes.
LANGUAGES = {"auto": "自动检测", "de": "德语", "en": "英语", "fr": "法语", "ja": "日语", "es": "西班牙语"}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
    except FileNotFoundError:
        pass
    except ValueError as exc:
        raise RuntimeError(f"网关配置 {CONFIG_PATH} 不是有效的 JSON：{exc}") from None
    return cfg


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.replace(f.name, path)
    finally:
        Path(f.name).unlink(missing_ok=True)


def save(cfg: dict) -> None:
    _write_text(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))


def cache_dir(cfg: dict) -> Path:
    return Path(cfg.get("cache_dir") or DATA_DIR / "cache")


# ---- shared glossary --------------------------------------------------------------------
def terms_path() -> Path:
    return DATA_DIR / "terms.txt"


def read_terms() -> str:
    try:
        return terms_path().read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""


def save_terms(raw: str) -> None:
    """Publish a complete glossary; running gateways pick it up on the next request."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with FileLock(str(DATA_DIR / "terms.lock"), timeout=5):
        _write_text(terms_path(), raw)


def terms_revision():
    try:
        info = terms_path().stat()
        return info.st_mtime_ns, info.st_size, info.st_ino
    except FileNotFoundError:
        return None
