"""Driving tracks (latitude / longitude over time) read from bags, without ROS.

Supported message types (CDR, decoded by hand; only the leading fields needed):

- ``autoware_adapi_v1_msgs/msg/VehicleKinematics``: ``geographic_pose`` (GeoPointStamped) comes first
- ``sensor_msgs/msg/NavSatFix``: messages without a fix (status < 0) are dropped
- ``geographic_msgs/msg/GeoPointStamped`` / ``GeoPoseStamped``
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import bagreader

MIN_STEP_M = 0.5        # drop points closer than this to the previous kept point...
MAX_GAP_S = 5.0         # ...unless this much time has passed
MAX_POINTS = 5000


# ---------------------------------------------------------------- CDR decoding


def _header_end(data: bytes) -> tuple[str, int]:
    """Byte order and offset just after a std_msgs/Header at the start of a CDR message."""
    if len(data) < 4 or data[0] != 0:
        raise ValueError("not a CDR message")
    order = "<" if data[1] & 1 else ">"
    (n,) = struct.unpack_from(order + "I", data, 12)  # after encapsulation(4) + stamp(8)
    return order, 16 + n


def _align(offset: int, n: int) -> int:
    # CDR alignment is relative to the end of the 4-byte encapsulation header.
    return 4 + (offset - 4 + n - 1) // n * n


def _geo_point_after_header(data: bytes) -> tuple[float, float, float] | None:
    order, o = _header_end(data)
    return struct.unpack_from(order + "ddd", data, _align(o, 8))


def _navsatfix(data: bytes) -> tuple[float, float, float] | None:
    order, o = _header_end(data)
    (status,) = struct.unpack_from(order + "b", data, o)  # NavSatStatus.status, then uint16 service
    if status < 0:
        return None
    return struct.unpack_from(order + "ddd", data, _align(o + 4, 8))


DECODERS: dict[str, Callable[[bytes], tuple[float, float, float] | None]] = {
    "autoware_adapi_v1_msgs/msg/VehicleKinematics": _geo_point_after_header,
    "sensor_msgs/msg/NavSatFix": _navsatfix,
    "geographic_msgs/msg/GeoPointStamped": _geo_point_after_header,
    "geographic_msgs/msg/GeoPoseStamped": _geo_point_after_header,
}
PRIORITY = list(DECODERS)  # preferred first


def candidates(topics: Iterable[sqlite3.Row | dict]) -> list[dict]:
    """Topics of a bag that a track can be read from, best first."""
    found = [{"name": t["name"], "type": t["type"], "message_count": t["message_count"]}
             for t in topics if t["type"] in DECODERS and t["message_count"]]
    return sorted(found, key=lambda t: (PRIORITY.index(t["type"]), -t["message_count"], t["name"]))


# ---------------------------------------------------------------- reading


@dataclass
class Track:
    topic: str
    type: str
    points: list[list[float]]  # [latitude, longitude, seconds since bag start]

    def to_dict(self) -> dict:
        return {"topic": self.topic, "type": self.type, "points": self.points}


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    dy = (b[0] - a[0]) * 111_320.0
    dx = (b[1] - a[1]) * 111_320.0 * math.cos(lat)
    return math.hypot(dx, dy)


def read_track(path: str | Path, topic: str, type_: str, bag_start_ns: int | None) -> Track:
    """Decode positions on ``topic`` and thin them out for display."""
    decode = DECODERS.get(type_)
    if decode is None:
        raise ValueError(f"unsupported message type for tracks: {type_}")
    points: list[list[float]] = []
    last: list[float] | None = None
    for _, t, data in bagreader.iter_messages(path, [topic]):
        try:
            pos = decode(data)
        except (struct.error, ValueError):
            continue
        if pos is None:
            continue
        lat, lon = pos[0], pos[1]
        if not (math.isfinite(lat) and math.isfinite(lon)) or (lat == 0 and lon == 0) \
                or abs(lat) > 90 or abs(lon) > 180:
            continue
        offset = (t - bag_start_ns) / 1e9 if bag_start_ns is not None else 0.0
        if last is not None and _distance_m((last[0], last[1]), (lat, lon)) < MIN_STEP_M \
                and offset - last[2] < MAX_GAP_S:
            continue
        last = [round(lat, 7), round(lon, 7), round(offset, 2)]
        points.append(last)
    if len(points) > MAX_POINTS:
        stride = math.ceil(len(points) / MAX_POINTS)
        points = points[::stride] + ([points[-1]] if (len(points) - 1) % stride else [])
    return Track(topic, type_, points)


# ---------------------------------------------------------------- cache


def get_track(conn: sqlite3.Connection, bag: sqlite3.Row | dict, topic: str | None = None) -> Track | None:
    """Track of a catalogued bag, from the cache when the bag has not changed.

    Without ``topic`` the best candidate topic is used. Returns None when the
    bag has no supported position topic.
    """
    from . import catalog

    options = candidates(catalog.topics_of(conn, bag["id"]))
    if topic is not None:
        options = [o for o in options if o["name"] == topic]
    if not options:
        return None
    chosen = options[0]
    row = conn.execute(
        "SELECT points FROM tracks WHERE bag_id = ? AND topic = ? AND mtime = ?",
        (bag["id"], chosen["name"], bag["mtime"]),
    ).fetchone()
    if row is not None:
        return Track(chosen["name"], chosen["type"], json.loads(row["points"]))
    track = read_track(bag["path"], chosen["name"], chosen["type"], bag["start_ns"])
    conn.execute(
        "INSERT OR REPLACE INTO tracks (bag_id, topic, mtime, points) VALUES (?, ?, ?, ?)",
        (bag["id"], chosen["name"], bag["mtime"], json.dumps(track.points, separators=(",", ":"))),
    )
    conn.commit()
    return track


def to_geojson(track: Track, bag: sqlite3.Row | dict) -> dict:
    coords = [[p[1], p[0]] for p in track.points]
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"bag": bag["path"], "topic": track.topic, "type": track.type,
                           "start_ns": bag["start_ns"], "offsets_s": [p[2] for p in track.points]}}
