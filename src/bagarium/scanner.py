"""Discover ROS 2 bags on disk and extract their metadata without ROS."""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

METADATA_FILE = "metadata.yaml"
BAG_SUFFIXES = {".mcap": "mcap", ".db3": "sqlite3"}


@dataclass
class TopicInfo:
    name: str
    type: str
    message_count: int


@dataclass
class BagInfo:
    path: Path
    storage: str
    size_bytes: int
    mtime: float
    start_ns: int | None = None
    end_ns: int | None = None
    message_count: int = 0
    topics: list[TopicInfo] = field(default_factory=list)

    @property
    def duration_ns(self) -> int | None:
        if self.start_ns is None or self.end_ns is None:
            return None
        return self.end_ns - self.start_ns


def discover(root: Path) -> Iterator[Path]:
    """Yield bag paths under ``root``.

    A directory containing ``metadata.yaml`` is one bag; its own data files are
    part of it, but subdirectories are still searched (e.g. ``derivatives/``).
    Any other ``.mcap`` / ``.db3`` file is treated as a standalone bag.
    """
    root = Path(root).resolve()
    if root.is_file():
        if root.suffix in BAG_SUFFIXES:
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if METADATA_FILE in filenames:
            yield Path(dirpath)
            continue
        for name in sorted(filenames):
            if Path(name).suffix in BAG_SUFFIXES:
                yield Path(dirpath) / name


def stat_bag(path: Path) -> tuple[int, float]:
    """Return (total size in bytes, latest mtime) of a bag file or directory.

    For a bag directory only the files directly inside it count, so nested
    bags (e.g. ``derivatives/``) are not included. The directory's own mtime is
    ignored too: it changes when something is added next to the bag's files.
    """
    if path.is_file():
        st = path.stat()
        return st.st_size, st.st_mtime
    size, mtime = 0, 0.0
    for entry in os.scandir(path):
        if entry.is_file():
            st = entry.stat()
            size += st.st_size
            mtime = max(mtime, st.st_mtime)
    return size, mtime


def storage_of(path: Path) -> str:
    return BAG_SUFFIXES.get(path.suffix, "unknown") if path.is_file() else "unknown"


def read_bag(path: Path) -> BagInfo:
    """Read metadata of a bag. Raises on unreadable bags."""
    path = Path(path).resolve()
    size, mtime = stat_bag(path)
    if path.is_dir():
        info = _read_metadata_yaml(path)
    elif path.suffix == ".mcap":
        info = _read_mcap(path)
    elif path.suffix == ".db3":
        info = _read_db3(path)
    else:
        raise ValueError(f"not a bag: {path}")
    info.size_bytes, info.mtime = size, mtime
    if info.message_count == 0:
        info.start_ns = info.end_ns = None
    return info


def _read_metadata_yaml(path: Path) -> BagInfo:
    with open(path / METADATA_FILE, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    meta = doc.get("rosbag2_bagfile_information")
    if not isinstance(meta, dict):
        raise ValueError(f"invalid {METADATA_FILE}: missing rosbag2_bagfile_information")

    topics = []
    for entry in meta.get("topics_with_message_count") or []:
        tm = entry.get("topic_metadata") or {}
        topics.append(TopicInfo(tm.get("name", ""), tm.get("type", ""), int(entry.get("message_count", 0))))

    start = int((meta.get("starting_time") or {}).get("nanoseconds_since_epoch", 0))
    duration = int((meta.get("duration") or {}).get("nanoseconds", 0))
    count = meta.get("message_count")
    return BagInfo(
        path=path,
        storage=meta.get("storage_identifier") or "unknown",
        size_bytes=0,
        mtime=0.0,
        start_ns=start,
        end_ns=start + duration,
        message_count=int(count) if count is not None else sum(t.message_count for t in topics),
        topics=topics,
    )


def _read_mcap(path: Path) -> BagInfo:
    from mcap.reader import NonSeekingReader, make_reader

    counts: dict[tuple[str, str], int] = defaultdict(int)
    start = end = None
    total = 0

    with open(path, "rb") as f:
        try:
            summary = make_reader(f).get_summary()
        except Exception:
            summary = None

        if summary is not None and summary.statistics is not None:
            st = summary.statistics
            for channel_id, n in st.channel_message_counts.items():
                channel = summary.channels.get(channel_id)
                if channel is None:
                    continue
                schema = summary.schemas.get(channel.schema_id)
                counts[(channel.topic, schema.name if schema else "")] += n
            for channel in summary.channels.values():
                schema = summary.schemas.get(channel.schema_id)
                counts.setdefault((channel.topic, schema.name if schema else ""), 0)
            total = st.message_count
            start, end = st.message_start_time, st.message_end_time
        else:
            # No summary section (e.g. recording was interrupted): scan linearly
            # and keep whatever could be read before the file ends.
            f.seek(0)
            try:
                # log_time_order=False: sorting would buffer everything and
                # yield nothing if the file is truncated.
                for schema, channel, message in NonSeekingReader(f).iter_messages(log_time_order=False):
                    counts[(channel.topic, schema.name if schema else "")] += 1
                    total += 1
                    t = message.log_time
                    start = t if start is None else min(start, t)
                    end = t if end is None else max(end, t)
            except Exception:
                if total == 0:
                    raise

    topics = [TopicInfo(name, type_, n) for (name, type_), n in sorted(counts.items())]
    return BagInfo(path, "mcap", 0, 0.0, start, end, total, topics)


def _read_db3(path: Path) -> BagInfo:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT t.name, t.type, COUNT(m.id) FROM topics t "
            "LEFT JOIN messages m ON m.topic_id = t.id GROUP BY t.id ORDER BY t.name"
        ).fetchall()
        start, end, total = conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM messages"
        ).fetchone()
    finally:
        conn.close()
    topics = [TopicInfo(name, type_, n) for name, type_, n in rows]
    return BagInfo(path, "sqlite3", 0, 0.0, start, end, total, topics)
