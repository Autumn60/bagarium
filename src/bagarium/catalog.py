"""Catalog operations shared by the CLI and the web UI."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
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

COMMIT_EVERY = 50        # bags
COMMIT_INTERVAL = 0.5    # seconds; keeps write transactions short for concurrent edits
PARALLEL_MIN = 16        # read bags in worker processes when at least this many need reading


def scan(
    conn: sqlite3.Connection,
    roots: Iterable[str | Path],
    force: bool = False,
    on_bag: ProgressCallback | None = None,
    workers: int | None = None,
) -> ScanResult:
    """Index every bag under ``roots``.

    Unchanged bags (same size and mtime) are skipped unless ``force``.
    Previously indexed bags under a root that no longer exist are flagged as
    missing; their tags and notes are kept so they survive a rescan.

    Bags that need reading are read in ``workers`` processes (default: up to 8)
    when there are many of them. Writes are committed in small batches.

    ``on_bag(path, status, done, total)`` is called after each bag. An
    exception raised from it aborts the scan while keeping what was indexed so
    far (missing bags are then not flagged, since the scan was incomplete).
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
    batch = _CommitBatcher(conn)

    def report(path: Path, status: str) -> None:
        nonlocal done
        done += 1
        batch.tick()
        if on_bag:
            on_bag(path, status, done, total)

    try:
        for root, found, paths in plan:
            to_read = []
            for bag_path in paths:
                check = _check(conn, bag_path, force, result)
                if isinstance(check, str):
                    report(bag_path, check)
                else:
                    to_read.append(check)
            for (bag_path, row, size, mtime), info, error, problem in _read_all(to_read, workers):
                report(bag_path, _store(conn, bag_path, row, size, mtime, info, error, problem, result))
            result.missing += _mark_missing(conn, root, {str(p) for p in found})
            conn.execute("UPDATE roots SET last_scanned_at = ? WHERE path = ?", (_now(), str(root)))
            batch.flush()
    finally:
        conn.commit()
    return result


class _CommitBatcher:
    def __init__(self, conn: sqlite3.Connection):
        self.conn, self.pending, self.last = conn, 0, time.monotonic()

    def tick(self) -> None:
        self.pending += 1
        if self.pending >= COMMIT_EVERY or time.monotonic() - self.last >= COMMIT_INTERVAL:
            self.flush()

    def flush(self) -> None:
        self.conn.commit()
        self.pending, self.last = 0, time.monotonic()


def _check(conn: sqlite3.Connection, path: Path, force: bool, result: ScanResult):
    """Cheap pre-check: a status string when done ("unchanged" / "error"), else what to read."""
    row = conn.execute("SELECT id, size_bytes, mtime, missing, error, checked_mtime FROM bags WHERE path = ?",
                       (str(path),)).fetchone()
    try:
        size, mtime = scanner.stat_bag(path)
    except OSError as e:
        result.errors.append((path, str(e)))
        return "error"
    if row and not force and not row["missing"] and row["error"] is None \
            and row["size_bytes"] == size and row["mtime"] == mtime and row["checked_mtime"] == mtime:
        result.unchanged += 1
        return "unchanged"
    return path, row, size, mtime


def _read_one(path: Path) -> tuple[scanner.BagInfo | None, str | None, str | None]:
    """(metadata, error reading it, integrity problem of the data files)."""
    problem = scanner.check_files(path)
    try:
        return scanner.read_bag(path), None, problem
    except Exception as e:  # corrupt or unsupported bags are still listed
        return None, f"{type(e).__name__}: {e}", problem


def _read_all(items: list, workers: int | None):
    """Yield ``(item, info, error, problem)`` for each item, reading bags in parallel when there are many."""
    workers = workers if workers is not None else min(8, os.cpu_count() or 1)
    if workers <= 1 or len(items) < PARALLEL_MIN:
        for item in items:
            yield (item, *_read_one(item[0]))
        return
    # "spawn": forking a multi-threaded process (e.g. the web server) is unsafe.
    pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
    try:
        futures = {pool.submit(_read_one, item[0]): item for item in items}
        for future in as_completed(futures):
            yield (futures[future], *future.result())
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _store(conn, path: Path, row, size: int, mtime: float, info, error, problem, result: ScanResult) -> str:
    if info is None:
        info = scanner.BagInfo(path, scanner.storage_of(path), size, mtime)
        result.errors.append((path, error))
    bag_id = row["id"] if row else None
    status = "updated" if row else "added"
    if bag_id is None and error is None:
        bag_id = _find_moved(conn, info)
        if bag_id is not None:
            status = "moved"
    _upsert(conn, bag_id, info, error, problem)
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


def _upsert(conn: sqlite3.Connection, bag_id: int | None, info: scanner.BagInfo, error: str | None,
            problem: str | None = None) -> int:
    values = (
        str(info.path), info.path.name, info.storage, info.start_ns, info.end_ns,
        info.message_count, info.size_bytes, info.mtime, error, _now(), problem, info.mtime,
    )
    if bag_id is None:
        cur = conn.execute(
            "INSERT INTO bags (path, name, storage, start_ns, end_ns, message_count, size_bytes, mtime, error,"
            " indexed_at, problem, checked_mtime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        bag_id = cur.lastrowid
    else:
        conn.execute(
            "UPDATE bags SET path = ?, name = ?, storage = ?, start_ns = ?, end_ns = ?, message_count = ?,"
            " size_bytes = ?, mtime = ?, error = ?, indexed_at = ?, problem = ?, checked_mtime = ?, missing = 0"
            " WHERE id = ?",
            (*values, bag_id),
        )
        conn.execute("DELETE FROM bag_topics WHERE bag_id = ?", (bag_id,))
    pairs = [(t.name, t.type) for t in info.topics]
    conn.executemany("INSERT OR IGNORE INTO topic_names (name, type) VALUES (?, ?)", pairs)
    conn.executemany(
        "INSERT OR REPLACE INTO bag_topics (bag_id, topic_id, message_count)"
        " SELECT ?, id, ? FROM topic_names WHERE name = ? AND type = ?",
        [(bag_id, t.message_count, t.name, t.type) for t in info.topics],
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


def relocate(conn: sqlite3.Connection, old: Path, new: Path) -> list[int]:
    """Point catalog entries at ``old`` (and bags nested inside it) to ``new``.

    Call after moving the files. Ids, tags and notes are kept; size and mtime
    are refreshed so the next scan sees the bags as unchanged.
    """
    old_s, new_s = str(old), str(new)
    prefix = old_s.rstrip("/") + "/"
    rows = conn.execute(
        "SELECT id, path FROM bags WHERE path = ? OR substr(path, 1, ?) = ?", (old_s, len(prefix), prefix)
    ).fetchall()
    for row in rows:
        path = Path(new_s + row["path"][len(old_s):])
        size, mtime = scanner.stat_bag(path) if path.exists() else (None, None)
        conn.execute(
            "UPDATE bags SET path = ?, name = ?, missing = ?, size_bytes = COALESCE(?, size_bytes),"
            " mtime = COALESCE(?, mtime) WHERE id = ?",
            (str(path), path.name, 0 if path.exists() else 1, size, mtime, row["id"]),
        )
    conn.commit()
    return [r["id"] for r in rows]


def under_root(conn: sqlite3.Connection, path: str | Path) -> bool:
    """Whether ``path`` is inside a registered root (and so will be rescanned)."""
    path = Path(path)
    return any(path == Path(r["path"]) or Path(r["path"]) in path.parents
               for r in conn.execute("SELECT path FROM roots"))


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
    root: str | Path | None = None,
    broken: bool = False,
) -> list[sqlite3.Row]:
    """Search bags. All given conditions must hold (AND).

    - ``query``: whitespace-separated words matched against name, path and note
    - ``tags``: bag must have every tag
    - ``topics`` / ``types``: bag must contain a matching topic name / message type
      (substring, or glob if the pattern contains ``*?[``)
    - ``since`` / ``until``: epoch ns; bag's time range must overlap [since, until)
    - ``root``: bag must be at or under this directory
    - ``broken``: only bags that could not be read or failed the integrity check
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
    for column, patterns in (("tn.name", topics), ("tn.type", types)):
        for pattern in patterns:
            # match the (few) distinct topic names first, then the bags having them
            clause, value = _pattern_clause(column, pattern)
            where.append("b.id IN (SELECT bt.bag_id FROM bag_topics bt JOIN topic_names tn"
                         f" ON tn.id = bt.topic_id WHERE {clause})")
            params.append(value)
    if broken:
        where.append("(b.error IS NOT NULL OR b.problem IS NOT NULL)")
    if root:
        root_s = str(Path(root).expanduser().resolve())
        prefix = root_s.rstrip("/") + "/"
        where.append("(b.path = ? OR substr(b.path, 1, ?) = ?)")
        params += [root_s, len(prefix), prefix]
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


def broken_reason(bag: sqlite3.Row | dict) -> str | None:
    """Why a bag cannot be processed (damaged data files or unreadable metadata), or None.

    A damaged file is reported first: it is usually what makes the bag unreadable.
    """
    if bag["problem"]:
        return f"broken: {bag['problem']}"
    if bag["error"]:
        return f"unreadable: {bag['error']}"
    return None


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
