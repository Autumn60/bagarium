"""Read raw messages from ROS 2 bags without ROS (sqlite3 / mcap, split, zstd-compressed)."""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from . import scanner
from .scanner import YAML_LOADER


@dataclass
class TopicMeta:
    name: str
    type: str
    serialization_format: str = "cdr"
    offered_qos_profiles: str = ""


@dataclass
class Layout:
    storage: str
    files: list[Path]
    file_compressed: bool = False
    message_compressed: bool = False
    topics: dict[str, TopicMeta] = field(default_factory=dict)  # from metadata.yaml, if any


def layout(src: Path) -> Layout:
    """Storage format, data files and compression of a bag file or directory."""
    src = Path(src)
    if src.is_file():
        return Layout(scanner.storage_of(src), [src])
    with open(src / scanner.METADATA_FILE, encoding="utf-8") as f:
        meta = (yaml.load(f, Loader=YAML_LOADER) or {})["rosbag2_bagfile_information"]
    fmt = meta.get("compression_format") or ""
    mode = (meta.get("compression_mode") or "").upper()
    if fmt and fmt != "zstd":
        raise ValueError(f"unsupported compression format: {fmt}")
    topics = {}
    for entry in meta.get("topics_with_message_count") or []:
        tm = entry.get("topic_metadata") or {}
        qos = tm.get("offered_qos_profiles", "")
        topics[tm.get("name", "")] = TopicMeta(
            tm.get("name", ""), tm.get("type", ""), tm.get("serialization_format", "cdr"),
            qos if isinstance(qos, str) else yaml.safe_dump(qos),
        )
    return Layout(
        storage=meta.get("storage_identifier") or "unknown",
        files=[src / p for p in meta.get("relative_file_paths") or []],
        file_compressed=bool(fmt) and mode == "FILE",
        message_compressed=bool(fmt) and mode == "MESSAGE",
        topics=topics,
    )


def decompress_file(src: Path, dest_dir: Path) -> Path:
    import zstandard

    dest = dest_dir / src.name.removesuffix(".zstd")
    with open(src, "rb") as fi, open(dest, "wb") as fo:
        zstandard.ZstdDecompressor().copy_stream(fi, fo)
    return dest


@contextmanager
def data_files(lay: Layout) -> Iterator[list[Path]]:
    """The bag's data files, decompressed into a temporary directory if needed."""
    if not lay.file_compressed:
        yield lay.files
        return
    with tempfile.TemporaryDirectory(prefix="bagarium-read-") as tmp:
        yield [decompress_file(f, Path(tmp)) for f in lay.files]


def iter_messages(
    src: str | Path,
    topics: Iterable[str],
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> Iterator[tuple[str, int, bytes]]:
    """Yield ``(topic, timestamp_ns, serialized data)`` for the given topics, in time order per file."""
    src = Path(src)
    topics = list(topics)
    lay = layout(src)
    decompress = None
    if lay.message_compressed:
        import zstandard

        dctx = zstandard.ZstdDecompressor()
        decompress = lambda data: dctx.decompressobj().decompress(data)  # noqa: E731

    with data_files(lay) as files:
        for path in files:
            if lay.storage == "sqlite3":
                yield from _iter_sqlite(path, topics, start_ns, end_ns, decompress)
            elif lay.storage == "mcap":
                yield from _iter_mcap(path, topics, start_ns, end_ns)
            else:
                raise ValueError(f"unsupported storage: {lay.storage}")


def _iter_sqlite(path, topics, start_ns, end_ns, decompress):
    conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    try:
        ids = {tid: name for tid, name in conn.execute(
            f"SELECT id, name FROM topics WHERE name IN ({','.join('?' * len(topics))})", topics)}
        if not ids:
            return
        sql = f"SELECT topic_id, timestamp, data FROM messages WHERE topic_id IN ({','.join('?' * len(ids))})"
        params: list = list(ids)
        if start_ns is not None:
            sql += " AND timestamp >= ?"
            params.append(start_ns)
        if end_ns is not None:
            sql += " AND timestamp <= ?"
            params.append(end_ns)
        for tid, t, data in conn.execute(sql + " ORDER BY timestamp", params):
            yield ids[tid], t, decompress(data) if decompress else data
    finally:
        conn.close()


def _iter_mcap(path, topics, start_ns, end_ns):
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        for _, channel, message in make_reader(f).iter_messages(
            topics=topics, start_time=start_ns, end_time=None if end_ns is None else end_ns + 1,
        ):
            yield channel.topic, message.log_time, message.data
