"""SQLite storage for the bag catalog."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bags (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    storage       TEXT    NOT NULL,
    start_ns      INTEGER,
    end_ns        INTEGER,
    message_count INTEGER NOT NULL DEFAULT 0,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    mtime         REAL    NOT NULL DEFAULT 0,
    note          TEXT    NOT NULL DEFAULT '',
    missing       INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    indexed_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bags_start ON bags(start_ns);

CREATE TABLE IF NOT EXISTS topics (
    bag_id        INTEGER NOT NULL REFERENCES bags(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    type          TEXT    NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bag_id, name, type)
);
CREATE INDEX IF NOT EXISTS idx_topics_name ON topics(name);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS bag_tags (
    bag_id INTEGER NOT NULL REFERENCES bags(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (bag_id, tag_id)
);
"""


def default_db_path() -> Path:
    env = os.environ.get("BAGARIUM_DB")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "bagarium" / "bagarium.db"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path).expanduser() if path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Web requests may resolve the dependency and run the handler on different
    # worker threads; each request still gets its own connection.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
