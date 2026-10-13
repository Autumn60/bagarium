import json
import sqlite3
import struct

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from bagarium import catalog, track
from bagarium.cli import main
from bagarium.web import create_app

from conftest import T0
from test_cli_web import wait_for_job

S = 1_000_000_000
KIN = "autoware_adapi_v1_msgs/msg/VehicleKinematics"
FIX = "sensor_msgs/msg/NavSatFix"


def cdr(fields: list[tuple[str, object]], big_endian: bool = False) -> bytes:
    """Tiny CDR writer: fields are (struct format char or 'string', value)."""
    order = ">" if big_endian else "<"
    out = bytearray(b"\x00\x00\x00\x00" if big_endian else b"\x00\x01\x00\x00")
    for kind, value in fields:
        if kind == "string":
            data = value.encode() + b"\x00"
            out += b"\x00" * (-(len(out) - 4) % 4) + struct.pack(order + "I", len(data)) + data
        else:
            size = struct.calcsize(kind)
            out += b"\x00" * (-(len(out) - 4) % size) + struct.pack(order + kind, value)
    return bytes(out)


def header(t_ns, frame):
    return [("i", t_ns // S), ("I", t_ns % S), ("string", frame)]


def kinematics(t_ns, lat, lon, big_endian=False):
    # GeoPointStamped first; the rest (pose, twist, accel) is irrelevant here
    return cdr(header(t_ns, "global") + [("d", lat), ("d", lon), ("d", 10.0), ("d", 1.0)], big_endian)


def navsatfix(t_ns, lat, lon, status=0):
    return cdr(header(t_ns, "gnss") + [("b", status), ("H", 1), ("d", lat), ("d", lon), ("d", 5.0)]
               + [("d", 0.0)] * 9 + [("B", 0)])


def make_position_bag(path, messages):
    """messages: [(topic, type, t_ns, data)] -> standalone .db3"""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT, serialization_format TEXT, offered_qos_profiles TEXT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER, timestamp INTEGER, data BLOB);")
    ids = {}
    for topic, type_, t, data in messages:
        if topic not in ids:
            ids[topic] = conn.execute("INSERT INTO topics (name, type, serialization_format, offered_qos_profiles)"
                                      " VALUES (?, ?, 'cdr', '')", (topic, type_)).lastrowid
        conn.execute("INSERT INTO messages (topic_id, timestamp, data) VALUES (?, ?, ?)", (ids[topic], t, data))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def drive(tmp_path):
    """A 60 s drive heading north at ~1 m/s, with kinematics at 10 Hz and GNSS at 1 Hz (no fix for 10 s)."""
    msgs = []
    for i in range(600):
        t = T0 + i * S // 10
        msgs.append(("/api/vehicle/kinematics", KIN, t, kinematics(t, 33.0 + i * 0.1 / 111_320, 131.5)))
    for i in range(60):
        t = T0 + i * S
        msgs.append(("/fix", FIX, t, navsatfix(t, 33.0 + i / 111_320, 131.5, status=-1 if 20 <= i < 30 else 0)))
    msgs.append(("/chatter", "std_msgs/msg/String", T0, b"\x00\x01\x00\x00"))
    return make_position_bag(tmp_path / "drive.db3", msgs)


def test_decoders_both_byte_orders():
    t = T0 + 123
    assert track.DECODERS[KIN](kinematics(t, 33.07, 131.68)) == (33.07, 131.68, 10.0)
    assert track.DECODERS[KIN](kinematics(t, -1.5, 2.5, big_endian=True))[:2] == (-1.5, 2.5)
    assert track.DECODERS[FIX](navsatfix(t, 1.0, 2.0))[:2] == (1.0, 2.0)
    assert track.DECODERS[FIX](navsatfix(t, 1.0, 2.0, status=-1)) is None
    # frame ids of every length exercise the alignment padding
    for n in range(1, 10):
        data = cdr(header(t, "x" * n) + [("d", 7.0), ("d", 8.0), ("d", 9.0)])
        assert track.DECODERS[KIN](data) == (7.0, 8.0, 9.0)


def test_candidates_order():
    topics = [{"name": "/fix", "type": FIX, "message_count": 5}, {"name": "/k", "type": KIN, "message_count": 5},
              {"name": "/empty", "type": KIN, "message_count": 0}, {"name": "/x", "type": "std_msgs/msg/String", "message_count": 5}]
    assert [c["name"] for c in track.candidates(topics)] == ["/k", "/fix"]


def test_read_track_thins_and_skips_no_fix(drive):
    t = track.read_track(drive, "/api/vehicle/kinematics", KIN, T0)
    # 0.1 m steps thinned to >= 0.5 m: every 5th or 6th point (rounding), plus the first
    assert 100 <= len(t.points) <= 121
    assert t.points[0] == [33.0, 131.5, 0.0] and t.points[-1][2] == pytest.approx(59.5, abs=0.5)
    fix = track.read_track(drive, "/fix", FIX, T0)
    assert len(fix.points) == 50 and not any(20 <= p[2] < 30 for p in fix.points)


def test_get_track_cache(conn, drive):
    catalog.scan(conn, [drive])
    bag = catalog.get_bag(conn, drive)
    t = track.get_track(conn, bag)
    assert t.topic == "/api/vehicle/kinematics"
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    # cached: no need to read the bag again
    drive_bytes = drive.read_bytes()
    drive.unlink()
    assert track.get_track(conn, bag).points == t.points
    drive.write_bytes(drive_bytes)
    assert track.get_track(conn, bag, "/fix").topic == "/fix"
    assert track.get_track(conn, bag, "/chatter") is None
    # a changed bag is read again
    catalog.scan(conn, [drive])
    bag = catalog.get_bag(conn, drive)
    track.get_track(conn, bag)
    assert {r[0] for r in conn.execute("SELECT mtime FROM tracks")} >= {bag["mtime"]}


def test_cli_track(tmp_path, drive):
    dbp = tmp_path / "c.db"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(dbp), "scan", str(drive)])
    r = runner.invoke(main, ["--db", str(dbp), "track", str(drive)])
    assert r.exit_code == 0, r.output
    feature = json.loads(r.output)["features"][0]
    assert feature["geometry"]["coordinates"][0] == [131.5, 33.0]
    r = runner.invoke(main, ["--db", str(dbp), "track", "--format", "csv", "--topic", "/fix", str(drive)])
    lines = r.output.splitlines()
    assert lines[0].startswith("bag,topic,offset_s") and len(lines) == 51


def test_web_track(tmp_path, drive, bag_root):
    dbp = tmp_path / "c.db"
    CliRunner().invoke(main, ["--db", str(dbp), "scan", str(drive), str(bag_root)])
    client = TestClient(create_app(dbp))
    bags = {b["name"]: b["id"] for b in client.get("/api/bags").json()}

    page = client.get(f"/bags/{bags['drive.db3']}").text
    assert 'id="map"' in page and 'id="track-topic"' in page and "leaflet@1.9.4" in page
    assert 'id="map"' not in client.get(f"/bags/{bags['run_lidar']}").text

    body = client.get(f"/api/bags/{bags['drive.db3']}/track").json()
    assert body["topic"] == "/api/vehicle/kinematics" and len(body["points"]) > 100
    assert [c["name"] for c in body["candidates"]] == ["/api/vehicle/kinematics", "/fix"]
    assert client.get(f"/api/bags/{bags['drive.db3']}/track", params={"topic": "/fix"}).json()["topic"] == "/fix"
    assert client.get(f"/api/bags/{bags['drive.db3']}/track", params={"topic": "/nope"}).status_code == 404
    assert client.get(f"/api/bags/{bags['run_lidar']}/track").status_code == 404

    page = client.get("/map").text
    assert "1 of 5 bags have a position topic" in page and "drive.db3" in page
    assert "show on map" in client.get("/").text
    assert "None of these bags" in client.get("/map", params={"q": "run_lidar"}).text
