from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from mcap.writer import Writer

from bagarium import db

T0 = 1_760_000_000_000_000_000  # 2025-10-09T08:53:20Z


def make_mcap(path: Path, topics: dict[str, tuple[str, int]], start: int = T0, finish: bool = True) -> Path:
    """topics: {name: (type, count)}; messages are 0.1 s apart per topic."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        w = Writer(f, use_chunking=finish)
        w.start(profile="ros2", library="bagarium-tests")
        for name, (type_, count) in topics.items():
            sid = w.register_schema(name=type_, encoding="ros2msg", data=b"")
            cid = w.register_channel(topic=name, message_encoding="cdr", schema_id=sid)
            for i in range(count):
                t = start + i * 100_000_000
                w.add_message(channel_id=cid, log_time=t, publish_time=t, data=b"\x00")
        if finish:
            w.finish()
    return path


def make_db3(path: Path, topics: dict[str, tuple[str, int]], start: int = T0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT, serialization_format TEXT, offered_qos_profiles TEXT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER, timestamp INTEGER, data BLOB);"
    )
    for tid, (name, (type_, count)) in enumerate(topics.items(), 1):
        conn.execute("INSERT INTO topics VALUES (?, ?, ?, 'cdr', '')", (tid, name, type_))
        conn.executemany(
            "INSERT INTO messages (topic_id, timestamp, data) VALUES (?, ?, x'00')",
            [(tid, start + i * 100_000_000) for i in range(count)],
        )
    conn.commit()
    conn.close()
    return path


def make_bag_dir(path: Path, topics: dict[str, tuple[str, int]], start: int = T0, storage: str = "mcap") -> Path:
    """A `ros2 bag record` style directory with metadata.yaml."""
    path.mkdir(parents=True, exist_ok=True)
    fname = f"{path.name}_0.{'mcap' if storage == 'mcap' else 'db3'}"
    (make_mcap if storage == "mcap" else make_db3)(path / fname, topics, start)
    duration = max((c - 1) * 100_000_000 for _, c in topics.values())
    meta = {"rosbag2_bagfile_information": {
        "version": 5,
        "storage_identifier": storage,
        "duration": {"nanoseconds": duration},
        "starting_time": {"nanoseconds_since_epoch": start},
        "message_count": sum(c for _, c in topics.values()),
        "topics_with_message_count": [
            {"topic_metadata": {"name": n, "type": t, "serialization_format": "cdr", "offered_qos_profiles": ""},
             "message_count": c}
            for n, (t, c) in topics.items()
        ],
        "relative_file_paths": [fname],
    }}
    (path / "metadata.yaml").write_text(yaml.safe_dump(meta))
    return path


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "catalog.db")
    yield c
    c.close()


@pytest.fixture
def bag_root(tmp_path) -> Path:
    root = tmp_path / "bags"
    make_bag_dir(root / "2025-10-09" / "run_lidar", {
        "/lidar/points": ("sensor_msgs/msg/PointCloud2", 11),
        "/tf": ("tf2_msgs/msg/TFMessage", 21),
    })
    make_bag_dir(root / "2025-10-09" / "run_db3", {"/chatter": ("std_msgs/msg/String", 5)},
                 start=T0 + 3600 * 10**9, storage="sqlite3")
    make_mcap(root / "loose" / "camera.mcap", {
        "/camera/front/image_raw": ("sensor_msgs/msg/Image", 30),
        "/camera/rear/image_raw": ("sensor_msgs/msg/Image", 30),
    }, start=T0 + 86400 * 10**9)
    make_db3(root / "loose" / "old.db3", {"/odom": ("nav_msgs/msg/Odometry", 50)}, start=T0 - 86400 * 10**9)
    (root / ".hidden").mkdir()
    make_mcap(root / ".hidden" / "ignored.mcap", {"/x": ("std_msgs/msg/Empty", 1)})
    (root / "loose" / "readme.txt").write_text("not a bag")
    return root
