"""Move bags on disk and keep the catalog (ids, tags, notes) pointing at them."""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import catalog, fileops, scanner

ProgressCallback = Callable[[float], None]


@dataclass
class MoveItem:
    bag: dict
    target: Path
    status: str = "pending"  # pending / skipped / done / failed
    message: str = ""


def plan_moves(
    conn: sqlite3.Connection,
    bags: Iterable[sqlite3.Row | dict],
    dest: str | Path,
    taken: Iterable[Path] = (),
) -> list[MoveItem]:
    """Work out where each bag goes, like ``mv``.

    If ``dest`` is an existing directory (or several bags are given) each bag
    moves into it under its own name. A single bag with a ``dest`` that does
    not exist yet is moved *to* ``dest`` (i.e. renamed); a standalone file
    keeps its extension so it is still recognised as a bag.
    """
    bags = [dict(b) for b in bags]
    dest = Path(dest).expanduser().resolve()
    rename = len(bags) == 1 and not dest.is_dir()
    taken = set(taken)
    items = []
    for bag in bags:
        src = Path(bag["path"])
        if rename:
            target = dest
            if src.suffix in scanner.BAG_SUFFIXES and target.suffix != src.suffix:
                target = target.with_name(target.name + src.suffix)
        else:
            target = dest / src.name
        item = MoveItem(bag, target)
        items.append(item)
        item.message = _skip_reason(conn, src, target, taken) or ""
        if item.message:
            item.status = "skipped"
        else:
            taken.add(target)
    return items


def _skip_reason(conn: sqlite3.Connection, src: Path, target: Path, taken: set[Path]) -> str | None:
    if not src.exists():
        return "files not found"
    if target == src:
        return "already there"
    if src in target.parents:
        return "cannot move a bag into itself"
    if target in taken:
        return "another bag is already going to this destination"
    if target.exists():
        return f"destination already exists: {target}"
    if conn.execute("SELECT 1 FROM bags WHERE path = ?", (str(target),)).fetchone():
        return f"catalog already has a bag at {target}; forget or prune it first"
    return None


def move_bag(conn: sqlite3.Connection, bag_id: int, target: str | Path,
             on_progress: ProgressCallback | None = None) -> str:
    """Move a catalogued bag to ``target`` and update the catalog.

    Within a filesystem this is a rename. Across filesystems the bag is copied
    next to ``target``, checked, put in place, and only then is the source
    removed; if anything fails before that, the source is untouched.
    ``on_progress(fraction)`` may raise to abort the copy.
    Returns a warning message, or "" when everything went fine.
    """
    row = catalog.get_bag(conn, bag_id)
    if row is None:
        raise ValueError(f"bag #{bag_id} is not in the catalog")
    src, target = Path(row["path"]), Path(target)
    reason = _skip_reason(conn, src, target, set())
    if reason:
        raise ValueError(reason)

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, target)
        copied = False
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        _copy(src, target, on_progress)
        copied = True

    catalog.relocate(conn, src, target)
    if copied:
        try:
            shutil.rmtree(src) if src.is_dir() else src.unlink()
        except OSError as e:
            return f"copied, but could not remove the source: {e}"
    if on_progress:
        on_progress(1.0)
    return ""


def _copy(src: Path, target: Path, on_progress: ProgressCallback | None) -> None:
    files = fileops.bag_files(src, recursive=True)
    total = sum(f.stat().st_size for f in files) or 1
    copied = 0

    def on_bytes(n: int) -> None:
        nonlocal copied
        copied += n
        if on_progress:
            on_progress(min(copied / total, 0.99))

    fileops.copy_staged(src, target, files, on_bytes)


def run_moves(
    conn: sqlite3.Connection,
    items: list[MoveItem],
    on_progress: Callable[[int, int, MoveItem, float], None] | None = None,
    on_item: Callable[[MoveItem], None] | None = None,
) -> None:
    """Move every pending item in turn; a failure is recorded and the rest go on.

    ``on_progress(k, n, item, fraction)`` may raise to abort the whole run.
    """
    todo = [i for i in items if i.status == "pending"]
    for k, item in enumerate(todo):
        raised: list[BaseException] = []

        def progress(frac: float, k=k, item=item) -> None:
            if on_progress is None:
                return
            try:
                on_progress(k, len(todo), item, frac)
            except BaseException as e:
                raised.append(e)
                raise

        progress(0.0)
        try:
            warning = move_bag(conn, item.bag["id"], item.target, progress)
            item.status, item.message = "done", warning
        except Exception as e:
            if raised and e is raised[0]:
                raise
            item.status, item.message = "failed", str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
        if on_item:
            on_item(item)
