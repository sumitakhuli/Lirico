"""Where Lirico keeps its settings, cache and log.

Default: %USERPROFILE%\\.lirico (override with the LIRICO_HOME env var).
We avoid %APPDATA%/%LOCALAPPDATA% on purpose: the Microsoft Store build of
Python silently redirects writes there into its package sandbox, so
Explorer (and you) would never find the files.
"""

import os
from pathlib import Path


def home() -> Path:
    root = Path(os.environ.get("LIRICO_HOME") or (Path.home() / ".lirico"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_dir() -> Path:
    return home()


def cache_dir() -> Path:
    path = home() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_file() -> Path:
    return home() / "lirico.log"


def speech_model_dir():
    """Bundled Whisper tiny model: inside the exe, or ./models in a source checkout."""
    import sys
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    path = base / "models" / "whisper-tiny"
    return path if (path / "model.bin").exists() else None
