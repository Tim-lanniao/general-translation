"""Test helpers: every test gets its own data root."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from translation_gateway import config


def isolated_data(case) -> Path:
    """Point the gateway's data directory at a fresh folder; returns ``<root>/gateway``."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    for key, value in (("ROOT", root), ("DATA_DIR", root / "gateway"), ("CONFIG_PATH", root / "gateway" / "config.json"),
                       ("LOG_DIR", root / "gateway" / "logs")):
        p = patch.object(config, key, value)
        p.start()
        case.addCleanup(p.stop)
    return root / "gateway"
