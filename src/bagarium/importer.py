"""Import bags from another disk (e.g. an SSD) by copying them into a local folder.

Sources are left untouched. The folder layout below the source is kept, bags
already in the catalog (same time range, message count and size) are skipped,
so importing the same disk again only copies what is new.
"""

from __future__ import annotations

import fnmatch
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import catalog, fileops, scanner


@dataclass
class ImportItem:
    source: Path
    target: Path
    size: int
    status: str = "pending"  # pending / skipped / done / failed
    message: str = ""
    result_id: int | None = None


def plan_import(
    conn: sqlite3.Connection,
    src: str | Path,
    dest: str | Path,
    taken: Iterable[Path] = (),
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
) -> list[ImportItem]:
    """List the bags under ``src`` and decide where each goes under ``dest``.

    Bags whose data files fail the integrity check are skipped (unless ``src``
    is that single bag).

    ``include`` / ``exclude`` are glob patterns matched against each bag's path
    relative to ``src`` (e.g. ``2026-09-*/*``); bags not selected are left out.
    """
    src = Path(src).expanduser().resolve()
    dest = Path(dest).expanduser().resolve()
    if not src.exists():
        raise ValueError(f"source does not exist: {src}")
    if dest == src or src in dest.parents:
        raise ValueError("destination is inside the source")
    if dest in src.parents:
        raise ValueError("source is already inside the destination")

    # A source that is itself a bag keeps its own name under dest.
    base = src.parent if src.is_file() or (src / scanner.METADATA_FILE).is_file() else src
    taken = set(taken)
    include, exclude = list(include), list(exclude)
    single = src.is_file() or (src / scanner.METADATA_FILE).is_file()
    items = []
    for bag in scanner.discover(src):
        rel = bag.relative_to(base).as_posix()
        if (include and not any(fnmatch.fnmatchcase(rel, p) for p in include)) \
                or any(fnmatch.fnmatchcase(rel, p) for p in exclude):
            continue
        target = dest / bag.relative_to(base)
        item = ImportItem(bag, target, sum(f.stat().st_size for f in fileops.bag_files(bag, recursive=False)))
        items.append(item)
        # damaged sources are not copied, unless that one bag was asked for explicitly
        problem = None if single else scanner.check_files(bag)
        item.message = f"broken: {problem}" if problem else (_skip_reason(conn, item, taken) or "")
        if item.message:
            item.status = "skipped"
        else:
            taken.add(target)
    return items


def _skip_reason(conn: sqlite3.Connection, item: ImportItem, taken: set[Path]) -> str | None:
    try:
        info = scanner.read_bag(item.source)
    except Exception:
        info = None  # unreadable bags are still copied; the catalog will flag them
    if info is not None and info.start_ns is not None:
        rows = conn.execute(
            "SELECT path FROM bags WHERE start_ns = ? AND end_ns = ? AND message_count = ? AND size_bytes = ?"
            " AND path != ?",
            (info.start_ns, info.end_ns, info.message_count, info.size_bytes, str(item.source)),
        ).fetchall()
        for row in rows:
            if Path(row["path"]).exists():
                return f"already imported: {row['path']}"
    if item.target in taken:
        return "another bag is already going to this destination"
    if item.target.exists() and not (item.source.is_dir() and fileops.can_merge_into(item.target)):
        return f"destination already exists: {item.target}"
    return None


def select(items: list[ImportItem], sources: Iterable[str | Path]) -> list[ImportItem]:
    """Keep only the items whose source is in ``sources``."""
    chosen = {str(Path(s)) for s in sources}
    return [i for i in items if str(i.source) in chosen]


def pending_bytes(items: Iterable[ImportItem]) -> int:
    return sum(i.size for i in items if i.status == "pending")


ImportProgress = Callable[[int, int, int, ImportItem], None]


def run_import(
    conn: sqlite3.Connection,
    items: list[ImportItem],
    on_progress: ImportProgress | None = None,
    on_item: Callable[[ImportItem], None] | None = None,
) -> None:
    """Copy every pending item and add it to the catalog; failures are recorded and the rest go on.

    ``on_progress(k, n, copied_bytes, item)`` reports overall progress and may
    raise to abort the whole import (the bag being copied is discarded).
    Tags and notes of sources that are in the catalog are carried over.
    """
    todo = [i for i in items if i.status == "pending"]
    copied = 0
    failed: list[Path] = []
    for k, item in enumerate(todo):
        raised: list[BaseException] = []

        def on_bytes(n: int, k=k, item=item) -> None:
            nonlocal copied
            copied += n
            if on_progress is None:
                return
            try:
                on_progress(k, len(todo), copied, item)
            except BaseException as e:
                raised.append(e)
                raise

        on_bytes(0)
        if any(f in item.source.parents for f in failed):
            item.status, item.message = "skipped", "the enclosing bag failed to import"
        else:
            try:
                fileops.copy_staged(item.source, item.target, fileops.bag_files(item.source, recursive=False), on_bytes)
                catalog.scan(conn, [item.target])
                new = catalog.get_bag(conn, str(item.target))
                item.result_id = new["id"]
                _carry_over_annotations(conn, item.source, new["id"])
                item.status = "done"
            except Exception as e:
                if raised and e is raised[0]:
                    raise
                item.status, item.message = "failed", f"{type(e).__name__}: {e}"
                failed.append(item.source)
        if on_item:
            on_item(item)


def _carry_over_annotations(conn: sqlite3.Connection, source: Path, new_id: int) -> None:
    old = catalog.get_bag(conn, str(source))
    if old is None:
        return
    tags = catalog.tags_of(conn, [old["id"]])[old["id"]]
    if tags:
        catalog.add_tags(conn, new_id, tags)
    if old["note"]:
        catalog.set_note(conn, new_id, old["note"])
