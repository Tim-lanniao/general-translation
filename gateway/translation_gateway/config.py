"""Gateway settings and data directory.

The gateway owns the model settings, the shared glossary, device tokens and the
translation cache. Its data live in ``<data root>/gateway``; the audio caption app keeps
its own settings next to it in ``<data root>/audio``.

Data root: ``%TRANSLATION_DATA%``, else the legacy ``%TEAMS_CAPTION_DATA%``, else
``<repo>/data``. A data root written by the single-package version is split on first use
(see :func:`migrate_legacy`).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from filelock import FileLock

REPO = Path(__file__).resolve().parents[2]
COMPONENT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("TRANSLATION_DATA") or os.environ.get("TEAMS_CAPTION_DATA") or REPO / "data")
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
# Settings both components take over from the legacy combined config.json.
SHARED_KEYS = ("cache_dir",)

# Language names for prompts; clients send ISO codes.
LANGUAGES = {"auto": "自动检测", "de": "德语", "en": "英语", "fr": "法语", "ja": "日语", "es": "西班牙语"}

log = logging.getLogger(__name__)


def load() -> dict:
    migrate_legacy()
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


# ---- one-time split of the single-package data root ---------------------------------------
def _move(src: Path, dst: Path) -> None:
    if not src.exists() or dst.exists():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:  # e.g. a log still open in an old process; it stays where it is
        log.warning("Could not move %s to %s: %s", src, dst, exc)


def migrate_legacy(root: Path | None = None) -> bool:
    """Split ``<root>/config.json`` and the files beside it into ``gateway/`` and ``audio/``.

    Runs once; the old file is kept as ``config.legacy.json``. Returns whether anything moved.
    """
    root = Path(root or ROOT)
    legacy = root / "config.json"
    loose = [root / n for n in ("gateway-terms.txt", "gateway-devices.json", "model-test.active", "会议记录")]
    if not legacy.exists() and not any(p.exists() for p in loose):
        return False
    gateway, audio = root / "gateway", root / "audio"
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "layout-migration.lock"), timeout=30):
        if legacy.exists():
            try:
                old = json.loads(legacy.read_text(encoding="utf-8-sig"))
            except ValueError:
                old = {}
            old = old if isinstance(old, dict) else {}
            if not (gateway / "config.json").exists():
                own = {k: old[k] for k in DEFAULTS if k in old and k != "topic"}
                _write_text(gateway / "config.json", json.dumps(own, ensure_ascii=False, indent=2))
            if not (audio / "config.json").exists():
                rest = {k: v for k, v in old.items() if k not in DEFAULTS or k in SHARED_KEYS or k == "topic"}
                _write_text(audio / "config.json", json.dumps(rest, ensure_ascii=False, indent=2))
            # Installs from before the shared glossary kept their terms in config.json.
            if not (root / "gateway-terms.txt").exists() and not (gateway / "terms.txt").exists() and old.get("terms"):
                _write_text(gateway / "terms.txt", old["terms"])
            os.replace(legacy, root / "config.legacy.json")
        _move(root / "gateway-terms.txt", gateway / "terms.txt")
        _move(root / "gateway-devices.json", gateway / "devices.json")
        _move(root / "model-test.active", gateway / "model-test.active")
        _move(root / "会议记录", audio / "会议记录")
        logs = root / "logs"
        if logs.is_dir():
            for f in logs.iterdir():
                _move(f, (gateway if f.name.startswith("translation-gateway") else audio) / "logs" / f.name)
        cache = root / "cache"  # only used when cache_dir was left empty
        if cache.is_dir():
            for f in cache.glob("translation-cache.sqlite3*"):
                _move(f, gateway / "cache" / f.name)
            for f in cache.iterdir():
                _move(f, audio / "cache" / f.name)
        for folder in (logs, cache):
            try:
                folder.rmdir()
            except OSError:
                pass
    log.info("Split the data directory %s into gateway/ and audio/", root)
    return True
