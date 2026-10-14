"""Where bagarium keeps its files.

Each location is chosen in this order:

1. its own variable: ``BAGARIUM_DB`` (catalog database), ``BAGARIUM_CACHE_DIR`` (thumbnails, previews)
2. ``BAGARIUM_HOME``: everything in one directory (``bagarium.db`` and ``cache/``)
3. the XDG defaults: ``~/.local/share/bagarium/bagarium.db`` and ``~/.cache/bagarium``
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def home() -> Path | None:
    return _env_path("BAGARIUM_HOME")


def db_path() -> Path:
    if (path := _env_path("BAGARIUM_DB")) is not None:
        return path
    if (base := home()) is not None:
        return base / "bagarium.db"
    data = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data) / "bagarium" / "bagarium.db"


def cache_dir() -> Path:
    if (path := _env_path("BAGARIUM_CACHE_DIR")) is not None:
        return path
    if (base := home()) is not None:
        return base / "cache"
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "bagarium"


def source(kind: str) -> str:
    """Which setting decided a location ("db" or "cache"), for `bagarium info`."""
    own = {"db": "BAGARIUM_DB", "cache": "BAGARIUM_CACHE_DIR"}[kind]
    if os.environ.get(own):
        return own
    if os.environ.get("BAGARIUM_HOME"):
        return "BAGARIUM_HOME"
    xdg = {"db": "XDG_DATA_HOME", "cache": "XDG_CACHE_HOME"}[kind]
    return xdg if os.environ.get(xdg) else "default"
