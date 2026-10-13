"""Catalog operations shared by the CLI and the web UI."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import scanner


@dataclass
class ScanResult:
    added: int = 0
    updated: int = 0
    moved: int = 0
    unchanged: int = 0
    missing: int = 0
    errors: list[tuple[Path, str]] = field(default_factory=list)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- scanning


ProgressCallback = Callable[[Path, str, int, int], None]


def scan(
    conn: sqlite3.Connection,
    roots: Iterable[str | Path],
    force: bool = False,
    on_bag: ProgressCallback | None = None,
) -> ScanResult:
    """Index every bag under ``roots``.

    Unchanged bags (same size and mtime) are skipped unless ``force``.
    Previously indexed bags under a root that no longer exist are flagged as
    missing; their tags and notes are kept so they survive a rescan.

    ``on_bag(path, status, done, total)`` is called after each bag. Each bag is
    committed on its own, so an exception raised from the callback aborts the
    scan while keeping what was indexed so far (missing bags are then not
    flagged, since the scan was incomplete).
    """
    result = ScanResult()
    # (root, all bags under root, bags to index here: not already planned by an overlapping root)
    plan: list[tuple[Path, list[Path], list[Path]]] = []
    seen: set[Path] = set()
    for root in roots:
        root = Path(root).expanduser().resolve()
        if not root.exists():
            result.errors.append((root, "path does not exist"))
            continue
        found = list(scanner.discover(root))
        paths = [p for p in found if p not in seen]
        seen.update(paths)
        plan.append((root, found, paths))

    total = sum(len(paths) for _, _, paths in plan)
    done = 0
    for root, found, paths in plan:
        for bag_path in paths:
            status = _index_one(conn, bag_path, force, result)
            conn.commit()
            done += 1
            if on_bag:
                on_bag(bag_path, status, done, total)
        result.missing += _mark_missing(conn, root, {str(p) for p in found})
        conn.execute("UPDATE roots SET last_scanned_at = ? WHERE path = ?", (_now(), str(root)))
        conn.commit()
    return result


def _index_one(conn: sqlite3.Connection, path: Path, force: bool, result: ScanResult) -> str:
    row = conn.execute("SELECT id, size_bytes, mtime, missing, error FROM bags WHERE path = ?", (str(path),)).fetchone()
    try:
        size, mtime = scanner.stat_bag(path)
    except OSError as e:
        result.errors.append((path, str(e)))
        return "error"
    if row and not force and not row["missing"] and row["error"] is None \
            and row["size_bytes"] == size and row["mtime"] == mtime:
        result.unchanged += 1
        return "unchanged"

    try:
        info = scanner.read_bag(path)
        error = None
    except Exception as e:  # corrupt or unsupported bags are still listed
        info = scanner.BagInfo(path, scanner.storage_of(path), size, mtime)
        error = f"{type(e).__name__}: {e}"
        result.errors.append((path, error))

    bag_id = row["id"] if row else None
    status = "updated" if row else "added"
    if bag_id is None and error is None:
        bag_id = _find_moved(conn, info)
        if bag_id is not None:
            status = "moved"
    _upsert(conn, bag_id, info, error)
    setattr(result, status, getattr(result, status) + 1)
    return "error" if error else status


def _find_moved(conn: sqlite3.Connection, info: scanner.BagInfo) -> int | None:
    """Find an indexed bag that disappeared from its old path and matches ``info``."""
    if info.start_ns is None:
        return None
    rows = conn.execute(
        "SELECT id, path FROM bags WHERE start_ns = ? AND end_ns = ? AND message_count = ? AND size_bytes = ?",
        (info.start_ns, info.end_ns, info.message_count, info.size_bytes),
    ).fetchall()
    for row in rows:
        if not Path(row["path"]).exists():
            return row["id"]
    return None


def _upsert(conn: sqlite3.Connection, bag_id: int | None, info: scanner.BagInfo, error: str | None) -> int:
    values = (
        str(info.path), info.path.name, info.storage, info.start_ns, info.end_ns,
        info.message_count, info.size_bytes, info.mtime, error, _now(),
    )
    if bag_id is None:
        cur = conn.execute(
            "INSERT INTO bags (path, name, storage, start_ns, end_ns, message_count, size_bytes, mtime, error, indexed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        bag_id = cur.lastrowid
    else:
        conn.execute(
            "UPDATE bags SET path = ?, name = ?, storage = ?, start_ns = ?, end_ns = ?, message_count = ?,"
            " size_bytes = ?, mtime = ?, error = ?, indexed_at = ?, missing = 0 WHERE id = ?",
            (*values, bag_id),
        )
        conn.execute("DELETE FROM topics WHERE bag_id = ?", (bag_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO topics (bag_id, name, type, message_count) VALUES (?, ?, ?, ?)",
        [(bag_id, t.name, t.type, t.message_count) for t in info.topics],
    )
    return bag_id


def _mark_missing(conn: sqlite3.Connection, root: Path, found: set[str]) -> int:
    prefix = str(root).rstrip("/") + "/"
    rows = conn.execute(
        "SELECT id, path FROM bags WHERE missing = 0 AND (path = ? OR substr(path, 1, ?) = ?)",
        (str(root), len(prefix), prefix),
    ).fetchall()
    gone = [r["id"] for r in rows if r["path"] not in found and not Path(r["path"]).exists()]
    conn.executemany("UPDATE bags SET missing = 1 WHERE id = ?", [(i,) for i in gone])
    return len(gone)


# ---------------------------------------------------------------- roots


def add_roots(conn: sqlite3.Connection, paths: Iterable[str | Path]) -> list[Path]:
    """Register directories to scan. Returns the resolved paths."""
    added = []
    for path in paths:
        path = Path(path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"not a directory: {path}")
        conn.execute("INSERT OR IGNORE INTO roots (path, added_at) VALUES (?, ?)", (str(path), _now()))
        added.append(path)
    conn.commit()
    return added


def remove_roots(conn: sqlite3.Connection, paths: Iterable[str | Path]) -> int:
    """Unregister directories. Indexed bags, tags and notes are kept."""
    n = 0
    for path in paths:
        n += conn.execute("DELETE FROM roots WHERE path = ?", (str(Path(path).expanduser().resolve()),)).rowcount
    conn.commit()
    return n


def list_roots(conn: sqlite3.Connection) -> list[dict]:
    """Registered roots with the number of (non-missing) bags indexed under each."""
    roots = []
    for row in conn.execute("SELECT * FROM roots ORDER BY path").fetchall():
        prefix = row["path"].rstrip("/") + "/"
        count = conn.execute(
            "SELECT COUNT(*) FROM bags WHERE missing = 0 AND (path = ? OR substr(path, 1, ?) = ?)",
            (row["path"], len(prefix), prefix),
        ).fetchone()[0]
        roots.append({**dict(row), "bag_count": count, "exists": Path(row["path"]).is_dir()})
    return roots


# ---------------------------------------------------------------- queries


def parse_time(text: str, end_of_day: bool = False) -> int:
    """Parse an ISO-like local date/time into epoch nanoseconds.

    A bare date used as an upper bound covers the whole day.
    """
    text = text.strip()
    dt = datetime.fromisoformat(text)
    if end_of_day and len(text) <= 10:
        dt += timedelta(days=1)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1_000_000_000)


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _pattern_clause(column: str, pattern: str) -> tuple[str, str]:
    """Glob match when the pattern has wildcards, otherwise substring match."""
    if any(c in pattern for c in "*?["):
        return f"{column} GLOB ?", pattern
    return f"{column} LIKE ? ESCAPE '\\'", _like(pattern)


def search(
    conn: sqlite3.Connection,
    query: str = "",
    tags: Sequence[str] = (),
    topics: Sequence[str] = (),
    types: Sequence[str] = (),
    since: int | None = None,
    until: int | None = None,
    include_missing: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Search bags. All given conditions must hold (AND).

    - ``query``: whitespace-separated words matched against name, path and note
    - ``tags``: bag must have every tag
    - ``topics`` / ``types``: bag must contain a matching topic name / message type
      (substring, or glob if the pattern contains ``*?[``)
    - ``since`` / ``until``: epoch ns; bag's time range must overlap [since, until)
    """
    where: list[str] = []
    params: list[object] = []
    if not include_missing:
        where.append("b.missing = 0")
    for word in query.split():
        where.append("(b.name LIKE ? ESCAPE '\\' OR b.path LIKE ? ESCAPE '\\' OR b.note LIKE ? ESCAPE '\\')")
        params += [_like(word)] * 3
    for tag in tags:
        where.append(
            "EXISTS (SELECT 1 FROM bag_tags bt JOIN tags t ON t.id = bt.tag_id WHERE bt.bag_id = b.id AND t.name = ?)"
        )
        params.append(tag)
    for column, patterns in (("tp.name", topics), ("tp.type", types)):
        for pattern in patterns:
            clause, value = _pattern_clause(column, pattern)
            where.append(f"EXISTS (SELECT 1 FROM topics tp WHERE tp.bag_id = b.id AND {clause})")
            params.append(value)
    if since is not None:
        where.append("b.end_ns >= ?")
        params.append(since)
    if until is not None:
        where.append("b.start_ns < ?")
        params.append(until)

    sql = "SELECT b.* FROM bags b"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.start_ns IS NULL, b.start_ns DESC, b.path"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_bag(conn: sqlite3.Connection, ref: str | int) -> sqlite3.Row | None:
    """Look up a bag by numeric id or by path."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = conn.execute("SELECT * FROM bags WHERE id = ?", (int(ref),)).fetchone()
        if row or isinstance(ref, int):
            return row
    path = str(Path(ref).expanduser().resolve())
    return conn.execute("SELECT * FROM bags WHERE path = ?", (path,)).fetchone()


def topics_of(conn: sqlite3.Connection, bag_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, type, message_count FROM topics WHERE bag_id = ? ORDER BY name", (bag_id,)
    ).fetchall()


def tags_of(conn: sqlite3.Connection, bag_ids: Iterable[int]) -> dict[int, list[str]]:
    ids = list(bag_ids)
    result: dict[int, list[str]] = {i: [] for i in ids}
    if not ids:
        return result
    placeholders = ",".join("?" * len(ids))
    for row in conn.execute(
        f"SELECT bt.bag_id, t.name FROM bag_tags bt JOIN tags t ON t.id = bt.tag_id"
        f" WHERE bt.bag_id IN ({placeholders}) ORDER BY t.name",
        ids,
    ):
        result[row[0]].append(row[1])
    return result


def all_tags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.name, COUNT(bt.bag_id) AS count FROM tags t"
        " LEFT JOIN bag_tags bt ON bt.tag_id = t.id GROUP BY t.id ORDER BY t.name"
    ).fetchall()


# ---------------------------------------------------------------- annotations


def normalize_tag(name: str) -> str:
    return " ".join(name.split())


def add_tags(conn: sqlite3.Connection, bag_id: int, names: Iterable[str]) -> None:
    for name in filter(None, map(normalize_tag, names)):
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        conn.execute(
            "INSERT OR IGNORE INTO bag_tags (bag_id, tag_id) SELECT ?, id FROM tags WHERE name = ?",
            (bag_id, name),
        )
    conn.commit()


def remove_tags(conn: sqlite3.Connection, bag_id: int, names: Iterable[str]) -> None:
    for name in map(normalize_tag, names):
        conn.execute(
            "DELETE FROM bag_tags WHERE bag_id = ? AND tag_id = (SELECT id FROM tags WHERE name = ?)",
            (bag_id, name),
        )
    _drop_unused_tags(conn)
    conn.commit()


def set_note(conn: sqlite3.Connection, bag_id: int, note: str) -> None:
    conn.execute("UPDATE bags SET note = ? WHERE id = ?", (note.strip(), bag_id))
    conn.commit()


def forget(conn: sqlite3.Connection, bag_ids: Iterable[int]) -> int:
    """Remove bags from the catalog (files on disk are untouched)."""
    n = 0
    for bag_id in bag_ids:
        n += conn.execute("DELETE FROM bags WHERE id = ?", (bag_id,)).rowcount
    _drop_unused_tags(conn)
    conn.commit()
    return n


def missing_ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM bags WHERE missing = 1")]


def _drop_unused_tags(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM bag_tags)")
