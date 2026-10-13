"""Safe file copying shared by moving and importing bags."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Iterable

CHUNK = 8 * 1024 * 1024


def bag_files(src: Path, recursive: bool) -> list[Path]:
    """Files making up a bag: the file itself, or the files in a bag directory.

    With ``recursive`` the files of nested directories (e.g. nested bags) are
    included too.
    """
    if src.is_file():
        return [src]
    if recursive:
        return sorted(Path(d) / f for d, _, fs in os.walk(src) for f in fs)
    return sorted(p for p in src.iterdir() if p.is_file())


def copy_staged(src: Path, target: Path, files: Iterable[Path],
                on_bytes: Callable[[int], None] | None = None) -> None:
    """Copy ``files`` of the bag at ``src`` so they end up at ``target``.

    Files are written to a hidden staging directory (ignored by scans) next to
    ``target``, their sizes are checked, and the result is renamed into place,
    so ``target`` either appears complete or not at all. Timestamps are kept.
    ``on_bytes(n)`` is called as data is written and may raise to abort.

    A bag directory may also be copied into an existing ``target`` that holds
    only subdirectories (e.g. a nested bag imported first); its files are then
    moved in one by one with ``metadata.yaml`` last, so the bag is only
    recognised once complete.
    """
    files = list(files)
    merge = src.is_dir() and can_merge_into(target)
    if target.exists() and not merge:
        raise FileExistsError(f"destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bagarium-copy-", dir=target.parent) as tmp:
        staged = Path(tmp) / target.name
        if src.is_dir():
            staged.mkdir()
        for f in files:
            dst = staged / f.relative_to(src) if src.is_dir() else staged
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "rb") as fi, open(dst, "wb") as fo:
                while chunk := fi.read(CHUNK):
                    fo.write(chunk)
                    if on_bytes:
                        on_bytes(len(chunk))
            shutil.copystat(f, dst)
            if dst.stat().st_size != f.stat().st_size:
                raise OSError(f"size mismatch after copying {f}")
        if src.is_dir():
            for d in sorted({staged / f.relative_to(src).parent for f in files}, key=lambda p: -len(p.parts)):
                shutil.copystat(src / d.relative_to(staged), d)
        if merge:
            for f in sorted(staged.iterdir(), key=lambda p: p.name == "metadata.yaml"):
                os.rename(f, target / f.name)
        else:
            os.rename(staged, target)


def can_merge_into(target: Path) -> bool:
    """Whether ``target`` is an existing directory without files of its own (only subdirectories)."""
    return target.is_dir() and not any(p.is_file() for p in target.iterdir())


def free_bytes(path: Path) -> int:
    """Free space on the filesystem that ``path`` is (or would be) created on."""
    path = Path(path)
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free
