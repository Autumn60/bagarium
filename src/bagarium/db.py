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
    indexed_at    TEXT    NOT NULL,
    problem       TEXT,               -- integrity problem of the data files (truncated, missing...)
    checked_mtime REAL                -- bag mtime the integrity check was done for
);
CREATE INDEX IF NOT EXISTS idx_bags_start ON bags(start_ns);

-- Topic names/types are shared by many bags, so they are stored once.
CREATE TABLE IF NOT EXISTS topic_names (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    UNIQUE (name, type)
);

CREATE TABLE IF NOT EXISTS bag_topics (
    bag_id        INTEGER NOT NULL REFERENCES bags(id) ON DELETE CASCADE,
    topic_id      INTEGER NOT NULL REFERENCES topic_names(id),
    message_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bag_id, topic_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_bag_topics_topic ON bag_topics(topic_id);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS roots (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    added_at        TEXT NOT NULL,
    last_scanned_at TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
    bag_id INTEGER NOT NULL REFERENCES bags(id) ON DELETE CASCADE,
    topic  TEXT    NOT NULL,
    mtime  REAL    NOT NULL,  -- bag mtime the track was computed for
    points TEXT    NOT NULL,  -- JSON [[lat, lon, seconds since bag start], ...]
    PRIMARY KEY (bag_id, topic)
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
    # The timeout lets writers wait for a concurrent scan's short transactions.
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Safe with WAL (no corruption); only the last commits may be lost on power failure.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(VIEWS)
    return conn


# Read-only view with the shape of the old `topics` table: (bag_id, name, type, message_count).
VIEWS = """
CREATE VIEW IF NOT EXISTS topics AS
    SELECT bt.bag_id AS bag_id, tn.name AS name, tn.type AS type, bt.message_count AS message_count
    FROM bag_topics bt JOIN topic_names tn ON tn.id = bt.topic_id;
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring catalogs made by earlier versions up to date."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(bags)")}
    for column, decl in (("problem", "TEXT"), ("checked_mtime", "REAL")):
        if column not in columns:  # never checked: the next scan re-reads and checks these bags
            conn.execute(f"ALTER TABLE bags ADD COLUMN {column} {decl}")
    conn.commit()

    # topic names normalized into topic_names / bag_topics
    row = conn.execute("SELECT type FROM sqlite_master WHERE name = 'topics'").fetchone()
    if row is None or row["type"] != "table":
        return
    with conn:
        conn.execute("INSERT OR IGNORE INTO topic_names (name, type) SELECT DISTINCT name, type FROM topics")
        conn.execute(
            "INSERT OR REPLACE INTO bag_topics (bag_id, topic_id, message_count)"
            " SELECT t.bag_id, n.id, t.message_count FROM topics t JOIN topic_names n USING (name, type)"
        )
        conn.execute("DROP TABLE topics")
    conn.execute("VACUUM")
