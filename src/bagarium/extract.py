"""Extract a time range and/or a subset of topics from a bag into a new bag.

The output is a ``ros2 bag`` directory (data file + metadata.yaml) in the same
storage format as the input: sqlite3 bags are written with the Humble-era
schema so every ROS 2 distro can play them, mcap bags keep their schemas and
channel metadata. Split bags are merged and compression is removed.
"""

from __future__ import annotations

import fnmatch
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import yaml

from . import __version__, bagreader, catalog, scanner
from .bagreader import TopicMeta

ProgressCallback = Callable[[float, int], None]
PROGRESS_EVERY = 2000

SQLITE_SCHEMA = """
CREATE TABLE schema(schema_version INTEGER PRIMARY KEY, ros_distro TEXT NOT NULL);
CREATE TABLE metadata(id INTEGER PRIMARY KEY, metadata_version INTEGER NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE topics(id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
                    serialization_format TEXT NOT NULL, offered_qos_profiles TEXT NOT NULL);
CREATE TABLE messages(id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL, timestamp INTEGER NOT NULL, data BLOB NOT NULL);
"""


@dataclass
class ExtractResult:
    path: Path
    storage: str
    message_count: int = 0
    start_ns: int | None = None
    end_ns: int | None = None
    topics: list[TopicMeta] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------- helpers


def parse_bag_time(text: str, bag_start_ns: int | None) -> int:
    """Parse a time given as an offset from the bag start or as an absolute time.

    Offsets: ``90``, ``+90.5``, ``1:30`` (m:s), ``1:02:03`` (h:m:s).
    Absolute: ``2026-09-02T09:30:00`` (local time) as accepted by ``catalog.parse_time``.
    """
    text = text.strip()
    if re.fullmatch(r"\+?\d+(\.\d*)?|\+?\d+(:\d{1,2}){1,2}(\.\d*)?", text):
        parts = text.lstrip("+").split(":")
        seconds = sum(float(p) * 60**i for i, p in enumerate(reversed(parts)))
        if bag_start_ns is None:
            raise ValueError("bag has no start time; use an absolute time")
        return bag_start_ns + round(seconds * 1e9)
    try:
        return catalog.parse_time(text)
    except ValueError:
        raise ValueError(f"invalid time: {text!r}") from None


def select_topics(names: Iterable[str], include: Iterable[str] = (), exclude: Iterable[str] = ()) -> list[str]:
    """Names matching any ``include`` pattern (all when empty) and no ``exclude`` pattern.

    Patterns match the whole name, with glob wildcards (``/camera/*``).
    """
    include, exclude = list(include), list(exclude)

    def hit(name: str, patterns: list[str]) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in patterns)

    return [n for n in names if (not include or hit(n, include)) and not hit(n, exclude)]


def default_suffix() -> str:
    return f"extract_{datetime.now():%Y%m%d-%H%M%S}"


def default_output_path(src: Path, output_dir: Path | None = None, suffix: str | None = None) -> Path:
    """``<output_dir or next to src>/<bag name without extension>_<suffix>``."""
    src = Path(src)
    stem = src.stem if src.suffix in scanner.BAG_SUFFIXES else src.name
    return (Path(output_dir) if output_dir else src.parent) / f"{stem}_{suffix or default_suffix()}"


# ---------------------------------------------------------------- extract


def extract(
    src: str | Path,
    out: str | Path,
    start_ns: int | None = None,
    end_ns: int | None = None,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    on_progress: ProgressCallback | None = None,
    bounds: tuple[int | None, int | None] | None = None,
) -> ExtractResult:
    """Write messages of ``src`` within [start_ns, end_ns] on the selected topics to a new bag at ``out``.

    ``bounds`` (bag start/end in ns) is only used for progress reporting and is
    read from the bag when omitted. ``on_progress(fraction, messages_written)``
    may raise to abort; nothing is left at ``out`` in that case.
    """
    src = Path(src).expanduser().resolve()
    out = Path(out).expanduser().resolve()
    if out.exists():
        raise FileExistsError(f"output already exists: {out}")
    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise ValueError("start is after end")

    layout = bagreader.layout(src)
    if layout.storage not in ("sqlite3", "mcap"):
        raise ValueError(f"unsupported storage: {layout.storage}")
    if bounds is None:
        info = scanner.read_bag(src)
        bounds = (info.start_ns, info.end_ns)
    lo = start_ns if start_ns is not None else bounds[0]
    hi = end_ns if end_ns is not None else bounds[1]

    def progress(t: int | None, written: int) -> None:
        if on_progress is None:
            return
        frac = 1.0 if t is None or lo is None or hi is None or hi <= lo else min(max((t - lo) / (hi - lo), 0.0), 1.0)
        on_progress(frac, written)

    def want(name: str) -> bool:
        return bool(select_topics([name], include, exclude))

    out.parent.mkdir(parents=True, exist_ok=True)
    # Work in a hidden sibling directory (skipped by scans, same filesystem as
    # the output) and move the finished bag into place at the end.
    with tempfile.TemporaryDirectory(prefix=".bagarium-extract-", dir=out.parent) as tmp:
        tmp = Path(tmp)
        files = [bagreader.decompress_file(f, tmp) for f in layout.files] if layout.file_compressed else layout.files
        stage = tmp / out.name
        stage.mkdir()
        ext = "db3" if layout.storage == "sqlite3" else "mcap"
        data_file = stage / f"{out.name}_0.{ext}"
        result = ExtractResult(out, layout.storage)

        if layout.storage == "sqlite3":
            _extract_sqlite(files, data_file, want, start_ns, end_ns, layout, result, progress)
        else:
            _extract_mcap(files, data_file, want, start_ns, end_ns, layout, result, progress)

        _write_metadata(stage, data_file.name, result)
        stage.rename(out)
    progress(None, result.message_count)
    return result


def _note_message(result: ExtractResult, topic: str, t: int) -> None:
    result.message_count += 1
    result.counts[topic] = result.counts.get(topic, 0) + 1
    result.start_ns = t if result.start_ns is None else min(result.start_ns, t)
    result.end_ns = t if result.end_ns is None else max(result.end_ns, t)


def _extract_sqlite(files, data_file, want, start_ns, end_ns, layout, result, progress) -> None:
    decompress = None
    if layout.message_compressed:
        import zstandard

        dctx = zstandard.ZstdDecompressor()
        decompress = lambda data: dctx.decompressobj().decompress(data)  # noqa: E731

    sources = [sqlite3.connect(f.as_uri() + "?mode=ro", uri=True) for f in files]
    out = sqlite3.connect(data_file)
    try:
        out.execute("PRAGMA journal_mode = OFF")
        out.execute("PRAGMA synchronous = OFF")
        out.executescript(SQLITE_SCHEMA)
        distro = "unknown"
        for s in sources:
            try:
                distro = s.execute("SELECT ros_distro FROM schema").fetchone()[0]
                break
            except (sqlite3.Error, TypeError):
                pass
        out.execute("INSERT INTO schema VALUES (3, ?)", (distro,))

        # Create output topics up front (in a stable order), including selected
        # topics that end up with no messages in the range, like rosbag2 does.
        out_ids: dict[tuple[str, str], int] = {}
        id_maps: list[dict[int, tuple[int, str]]] = []
        for s in sources:
            id_map = {}
            for tid, name, type_, fmt, qos in s.execute(
                "SELECT id, name, type, serialization_format, offered_qos_profiles FROM topics ORDER BY name"
            ):
                if not want(name):
                    continue
                key = (name, type_)
                if key not in out_ids:
                    out_ids[key] = out.execute(
                        "INSERT INTO topics (name, type, serialization_format, offered_qos_profiles) VALUES (?, ?, ?, ?)",
                        (name, type_, fmt, qos),
                    ).lastrowid
                    result.topics.append(TopicMeta(name, type_, fmt, qos))
                id_map[tid] = (out_ids[key], name)
            id_maps.append(id_map)
        if not out_ids:
            raise ValueError("no topics selected")

        insert = "INSERT INTO messages (topic_id, timestamp, data) VALUES (?, ?, ?)"
        batch: list[tuple[int, int, bytes]] = []
        for s, id_map in zip(sources, id_maps):
            if not id_map:
                continue
            sql = f"SELECT topic_id, timestamp, data FROM messages WHERE topic_id IN ({','.join('?' * len(id_map))})"
            params: list[int] = list(id_map)
            if start_ns is not None:
                sql += " AND timestamp >= ?"
                params.append(start_ns)
            if end_ns is not None:
                sql += " AND timestamp <= ?"
                params.append(end_ns)
            for tid, t, data in s.execute(sql + " ORDER BY timestamp", params):
                out_id, name = id_map[tid]
                batch.append((out_id, t, decompress(data) if decompress else data))
                _note_message(result, name, t)
                if len(batch) >= PROGRESS_EVERY:
                    out.executemany(insert, batch)
                    batch.clear()
                    progress(t, result.message_count)
        out.executemany(insert, batch)
        out.execute("CREATE INDEX timestamp_idx ON messages (timestamp ASC)")
        out.commit()
    finally:
        out.close()
        for s in sources:
            s.close()


def _extract_mcap(files, data_file, want, start_ns, end_ns, layout, result, progress) -> None:
    from mcap.reader import make_reader
    from mcap.writer import Writer

    with open(data_file, "wb") as fo:
        writer = Writer(fo)
        writer.start(profile="ros2", library=f"bagarium {__version__}")
        schema_ids: dict[tuple, int] = {}
        channel_ids: dict[tuple, int] = {}

        def out_channel(schema, channel) -> int:
            skey = (schema.name, schema.encoding, schema.data) if schema else None
            if skey is not None and skey not in schema_ids:
                schema_ids[skey] = writer.register_schema(name=schema.name, encoding=schema.encoding, data=schema.data)
            ckey = (channel.topic, channel.message_encoding, skey, tuple(sorted(channel.metadata.items())))
            if ckey not in channel_ids:
                channel_ids[ckey] = writer.register_channel(
                    topic=channel.topic, message_encoding=channel.message_encoding,
                    schema_id=schema_ids[skey] if skey else 0, metadata=dict(channel.metadata),
                )
                known = layout.topics.get(channel.topic)
                result.topics.append(TopicMeta(
                    channel.topic, schema.name if schema else (known.type if known else ""),
                    channel.message_encoding,
                    channel.metadata.get("offered_qos_profiles", known.offered_qos_profiles if known else ""),
                ))
            return channel_ids[ckey]

        any_topic = False
        for path in files:
            with open(path, "rb") as fi:
                reader = make_reader(fi)
                try:
                    summary = reader.get_summary()
                except Exception:
                    summary = None
                topics = None
                if summary is not None:
                    topics = sorted({c.topic for c in summary.channels.values() if want(c.topic)})
                    for channel in sorted(summary.channels.values(), key=lambda c: c.topic):
                        if channel.topic in topics:
                            out_channel(summary.schemas.get(channel.schema_id), channel)
                    if not topics:
                        continue
                any_topic = True
                for schema, channel, message in reader.iter_messages(
                    topics=topics, start_time=start_ns, end_time=None if end_ns is None else end_ns + 1,
                ):
                    if topics is None and not want(channel.topic):
                        continue
                    writer.add_message(
                        channel_id=out_channel(schema, channel), log_time=message.log_time,
                        publish_time=message.publish_time, sequence=message.sequence, data=message.data,
                    )
                    _note_message(result, channel.topic, message.log_time)
                    if result.message_count % PROGRESS_EVERY == 0:
                        progress(message.log_time, result.message_count)
        if not any_topic and not result.topics:
            raise ValueError("no topics selected")
        writer.finish()


def _write_metadata(stage: Path, file_name: str, result: ExtractResult) -> None:
    start = result.start_ns or 0
    duration = (result.end_ns - result.start_ns) if result.start_ns is not None else 0
    topics = [
        {
            "topic_metadata": {
                "name": t.name, "type": t.type,
                "serialization_format": t.serialization_format,
                "offered_qos_profiles": t.offered_qos_profiles,
            },
            "message_count": result.counts.get(t.name, 0),
        }
        for t in result.topics
    ]
    meta = {"rosbag2_bagfile_information": {
        "version": 5,
        "storage_identifier": result.storage,
        "duration": {"nanoseconds": duration},
        "starting_time": {"nanoseconds_since_epoch": start},
        "message_count": result.message_count,
        "topics_with_message_count": topics,
        "compression_format": "",
        "compression_mode": "",
        "relative_file_paths": [file_name],
        "files": [{
            "path": file_name,
            "starting_time": {"nanoseconds_since_epoch": start},
            "duration": {"nanoseconds": duration},
            "message_count": result.message_count,
        }],
    }}
    with open(stage / scanner.METADATA_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(meta, f, sort_keys=False, allow_unicode=True)


def extract_bag(
    conn: sqlite3.Connection,
    bag: sqlite3.Row,
    out: str | Path,
    start_ns: int | None = None,
    end_ns: int | None = None,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    on_progress: ProgressCallback | None = None,
    index: bool = True,
) -> tuple[ExtractResult, int | None]:
    """Extract from a catalogued bag and add the result to the catalog.

    Returns the result and the new bag's catalog id (None when ``index`` is False).
    """
    from . import fmt

    result = extract(bag["path"], out, start_ns, end_ns, include, exclude, on_progress,
                     bounds=(bag["start_ns"], bag["end_ns"]))
    if not index:
        return result, None
    catalog.scan(conn, [result.path])
    new = catalog.get_bag(conn, str(result.path))
    span = f"{fmt.time(start_ns if start_ns is not None else bag['start_ns'])} - " \
           f"{fmt.time(end_ns if end_ns is not None else bag['end_ns'])}"
    catalog.set_note(conn, new["id"], f"Extracted from {bag['path']} ({span}, {len(result.topics)} topics)")
    return result, new["id"]


# ---------------------------------------------------------------- batches


@dataclass
class BatchItem:
    bag: dict
    output: Path
    start_ns: int | None = None
    end_ns: int | None = None
    status: str = "pending"  # pending / skipped / done / failed
    message: str = ""
    result_id: int | None = None
    message_count: int = 0


def plan_batch(
    conn: sqlite3.Connection,
    bags: Iterable[sqlite3.Row | dict],
    start: str | None = None,
    end: str | None = None,
    duration: float | None = None,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    output_dir: str | Path | None = None,
    suffix: str | None = None,
    output: str | Path | None = None,
    taken: Iterable[Path] = (),
    skip_broken: bool | None = None,
) -> list[BatchItem]:
    """Decide output paths and time ranges for each bag, and which bags to skip.

    Times are resolved per bag, so offsets ("60", "1:30") count from each bag's
    own start. ``output`` names the exact output and is only valid for one bag;
    otherwise names are made unique within the batch, on disk and against
    ``taken`` (outputs other queued jobs will create).

    Broken bags (see ``catalog.broken_reason``) are skipped when several bags
    are given; a single bag is still tried, since part of it may be readable.
    """
    bags = [dict(b) for b in bags]
    if skip_broken is None:
        skip_broken = len(bags) > 1
    if output is not None and len(bags) != 1:
        raise ValueError("an exact output path needs exactly one bag; use an output directory")
    include, exclude = list(include), list(exclude)
    suffix = suffix or default_suffix()
    taken = set(taken)
    items = []
    for bag in bags:
        if output is not None:
            out = Path(output).expanduser().resolve()
            if out in taken:
                item = BatchItem(bag, out, status="skipped", message=f"output is already used by a queued job: {out}")
                items.append(item)
                continue
        else:
            out = _unique(default_output_path(Path(bag["path"]), output_dir, suffix).expanduser().resolve(), taken)
        item = BatchItem(bag, out)
        items.append(item)
        item.message = _skip_reason(conn, item, start, end, duration, include, exclude, skip_broken) or ""
        if item.message:
            item.status = "skipped"
    return items


def _skip_reason(conn, item: BatchItem, start, end, duration, include, exclude, skip_broken) -> str | None:
    bag = item.bag
    if bag["missing"]:
        return "files not found"
    if skip_broken and bag.get("problem"):
        return f"broken: {bag['problem']}"
    if bag["error"]:
        return "unreadable bag"
    if item.output.exists():
        return f"output already exists: {item.output}"
    try:
        item.start_ns = parse_bag_time(start, bag["start_ns"]) if start else None
        item.end_ns = parse_bag_time(end, bag["start_ns"]) if end else None
    except ValueError as e:
        return str(e)
    if duration is not None:
        base = item.start_ns if item.start_ns is not None else bag["start_ns"]
        if base is None:
            return "bag has no start time"
        item.end_ns = base + round(duration * 1e9)
    if item.start_ns is not None and item.end_ns is not None and item.start_ns > item.end_ns:
        return "start is after end"
    if bag["start_ns"] is not None and (
        (item.start_ns is not None and item.start_ns > bag["end_ns"])
        or (item.end_ns is not None and item.end_ns < bag["start_ns"])
    ):
        return "time range is outside the bag"
    names = [t["name"] for t in catalog.topics_of(conn, bag["id"])]
    if not select_topics(names, include, exclude):
        return "no topics match the given patterns"
    return None


def _unique(path: Path, taken: set[Path]) -> Path:
    candidate, n = path, 2
    while candidate in taken or candidate.exists():
        candidate = path.with_name(f"{path.name}_{n}")
        n += 1
    taken.add(candidate)
    return candidate


BatchProgress = Callable[[int, int, BatchItem, float, int], None]


def run_batch(
    conn: sqlite3.Connection,
    items: list[BatchItem],
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    on_progress: BatchProgress | None = None,
    on_item: Callable[[BatchItem], None] | None = None,
    index: bool = True,
) -> None:
    """Extract every pending item in turn. A failing bag is recorded and the batch goes on.

    ``on_progress(k, n, item, fraction, written)`` reports progress within the
    k-th of n pending items; an exception raised from it aborts the whole batch.
    """
    include, exclude = list(include), list(exclude)
    todo = [i for i in items if i.status == "pending"]
    for k, item in enumerate(todo):
        raised: list[BaseException] = []

        def progress(frac: float, written: int, k=k, item=item) -> None:
            if on_progress is None:
                return
            try:
                on_progress(k, len(todo), item, frac, written)
            except BaseException as e:
                raised.append(e)
                raise

        progress(0.0, 0)
        # The bag may have been moved since the batch was planned (queued jobs).
        fresh = catalog.get_bag(conn, item.bag["id"])
        if fresh is not None:
            item.bag = dict(fresh)
        try:
            result, item.result_id = extract_bag(conn, item.bag, item.output, item.start_ns, item.end_ns,
                                                 include, exclude, progress, index=index)
            item.status, item.message_count = "done", result.message_count
        except Exception as e:
            if raised and e is raised[0]:
                raise
            item.status, item.message = "failed", f"{type(e).__name__}: {e}"
        if on_item:
            on_item(item)
