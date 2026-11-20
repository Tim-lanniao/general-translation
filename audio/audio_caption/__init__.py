"""Audio translation: live Chinese captions for desktop audio, translated through the gateway."""
import sys
from pathlib import Path

try:
    import translation_gateway  # noqa: F401  (installed, or on PYTHONPATH)
except ImportError:  # running from a checkout: the gateway component sits next to this one
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway"))
