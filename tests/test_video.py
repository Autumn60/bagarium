import json
import shutil
import struct
import subprocess

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from bagarium import catalog, video
from bagarium.cli import main
from bagarium.web import create_app

from conftest import T0
from test_cli_web import wait_for_job
from test_track import cdr, header, make_position_bag

S = 1_000_000_000
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def with_bytes(fields, payload: bytes) -> bytes:
    """cdr() plus a trailing uint8[] sequence."""
    data = bytearray(cdr(fields))
    data += b"\x00" * (-(len(data) - 4) % 4) + struct.pack("<I", len(payload)) + payload
    return bytes(data)


def compressed(t, payload, fmt="jpeg"):
    return with_bytes(header(t, "cam") + [("string", fmt)], payload)


def raw_image(t, width, height, encoding, pixels):
    step = len(pixels) // height
    return with_bytes(header(t, "cam") + [("I", height), ("I", width), ("string", encoding), ("B", 0), ("I", step)],
                      pixels)


@pytest.fixture(scope="module")
def jpeg():
    if shutil.which("ffmpeg") is None:
        return b"\xff\xd8\xff\xe0fake\xff\xd9"
    return subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48", "-frames:v", "1",
                           "-f", "image2", "-c:v", "mjpeg", "-"], capture_output=True, check=True).stdout


@pytest.fixture
def camera_bag(tmp_path, jpeg):
    """3 s bag: a JPEG camera at 10 Hz from 0.5 s to 2.4 s, a raw rgb8 camera, and an empty depth topic."""
    msgs = [("/clock", "std_msgs/msg/String", T0, b"\x00\x01\x00\x00"),
            ("/clock", "std_msgs/msg/String", T0 + 3 * S, b"\x00\x01\x00\x00")]
    for i in range(20):
        t = T0 + S // 2 + i * S // 10
        msgs.append(("/sensing/camera/front/image_raw/compressed", video.COMPRESSED, t, compressed(t, jpeg)))
    for i in range(5):
        t = T0 + i * S // 2
        msgs.append(("/cam/raw", video.RAW, t, raw_image(t, 8, 6, "rgb8", bytes([i * 40]) * (8 * 6 * 3))))
    path = make_position_bag(tmp_path / "cams.db3", msgs)
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO topics (name, type, serialization_format, offered_qos_profiles)"
                 " VALUES ('/sensing/camera/front/image_raw/compressedDepth', ?, 'cdr', '')", (video.COMPRESSED,))
    conn.commit()
    return path


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                          "stream=width,height,r_frame_rate,nb_read_frames:format=duration", "-of", "json", str(path)],
                         capture_output=True, check=True, text=True).stdout
    return json.loads(out)


def test_decode_frames(jpeg):
    f = video.decode_frame(video.COMPRESSED, compressed(T0, jpeg))
    assert f.kind == "jpeg" and f.data == jpeg
    png = b"\x89PNG\r\n\x1a\nrest"
    assert video.decode_frame(video.COMPRESSED, compressed(T0, png, "png")).kind == "png"
    for n in range(1, 6):  # frame id lengths shift every later field's alignment
        f = video.decode_frame(video.RAW, with_bytes(
            [("i", 1), ("I", 2), ("string", "x" * n), ("I", 2), ("I", 3), ("string", "mono16"), ("B", 1), ("I", 6)],
            b"\x00" * 12))
        assert (f.kind, f.width, f.height, f.pix_fmt, len(f.data)) == ("raw", 3, 2, "gray16be", 12)
    with pytest.raises(ValueError, match="encoding"):
        video.decode_frame(video.RAW, raw_image(T0, 1, 1, "yuv422", b"\x00\x00"))
    with pytest.raises(ValueError, match="format"):
        video.decode_frame(video.COMPRESSED, compressed(T0, b"GIF89a"))


def test_camera_topics_and_names():
    topics = [{"name": "/sensing/camera/center/image_raw/compressed", "type": video.COMPRESSED, "message_count": 3},
              {"name": "/sensing/camera/center/image_raw/compressedDepth", "type": video.COMPRESSED, "message_count": 3},
              {"name": "/a/image", "type": video.RAW, "message_count": 0},
              {"name": "/b/image", "type": video.RAW, "message_count": 1}]
    assert [t["name"] for t in video.camera_topics(topics)] == ["/b/image", "/sensing/camera/center/image_raw/compressed"]
    assert video.short_name("/sensing/camera/center/image_raw/compressed") == "center"
    assert video.short_name("/front/left/image_rect_color") == "front/left/image_rect_color"


@needs_ffmpeg
def test_make_video_on_bag_clock(conn, camera_bag, tmp_path):
    catalog.scan(conn, [camera_bag])
    bag = catalog.get_bag(conn, camera_bag)
    out = video.make_video(camera_bag, "/sensing/camera/front/image_raw/compressed", video.COMPRESSED,
                           tmp_path / "v.mp4", bag["start_ns"], bag["end_ns"], 20)
    info = probe(out)
    stream = info["streams"][0]
    assert stream["r_frame_rate"] == "10/1" and (stream["width"], stream["height"]) == (64, 48)
    assert int(stream["nb_read_frames"]) == 31  # 0.0 .. 3.0 s at 10 fps: lead-in and tail are filled
    assert float(info["format"]["duration"]) == pytest.approx(3.1, abs=0.05)

    raw = video.make_video(camera_bag, "/cam/raw", video.RAW, tmp_path / "raw.mp4",
                           bag["start_ns"], bag["end_ns"], 5)
    assert probe(raw)["streams"][0]["r_frame_rate"] == "2/1"

    def stop(frac):
        if frac > 0.2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        video.make_video(camera_bag, "/cam/raw", video.RAW, tmp_path / "x.mp4", bag["start_ns"], bag["end_ns"], 5,
                         on_progress=stop)
    assert sorted(p.name for p in tmp_path.glob("*x.mp4*")) == []


@needs_ffmpeg
def test_web_cameras(tmp_path, camera_bag, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    dbp = tmp_path / "c.db"
    CliRunner().invoke(main, ["--db", str(dbp), "scan", str(camera_bag)])
    client = TestClient(create_app(dbp))
    topic = "/sensing/camera/front/image_raw/compressed"

    page = client.get("/bags/1").text
    section = page.split('id="cameras"')[1].split("</section>")[0]
    assert section.count('class="camera"') == 2 and "compressedDepth" not in section
    thumb = client.get("/bags/1/thumbnail", params={"topic": topic})
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert thumb.content[:2] == b"\xff\xd8"
    assert client.get("/bags/1/thumbnail", params={"topic": "/cam/raw"}).content[:2] == b"\xff\xd8"  # raw -> jpeg
    assert client.get("/bags/1/thumbnail", params={"topic": "/nope"}).status_code == 404

    assert client.get("/bags/1/video", params={"topic": topic}).status_code == 404
    r = client.post("/bags/1/video", data={"topic": topic})
    assert r.url.path == "/bags/1"
    job = wait_for_job(client)
    assert job["kind"] == "video" and job["state"] == "completed", job
    assert 'data-preview="true"' in client.get("/bags/1").text
    full = client.get("/bags/1/video", params={"topic": topic})
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
    part = client.get("/bags/1/video", params={"topic": topic}, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100  # seeking in the player
    client.post("/bags/1/video", data={"topic": topic})  # already there: nothing queued
    assert len(client.get("/api/jobs").json()) == 1


@needs_ffmpeg
def test_cli_video(tmp_path, camera_bag):
    dbp = tmp_path / "c.db"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(dbp), "scan", str(camera_bag)])
    r = runner.invoke(main, ["--db", str(dbp), "video", str(camera_bag), "--list"])
    assert r.output.splitlines()[0].startswith("/cam/raw\t")
    out = tmp_path / "front.mp4"
    r = runner.invoke(main, ["--db", str(dbp), "video", str(camera_bag), "--topic",
                             "/sensing/camera/front/image_raw/compressed", "-s", "1", "-e", "2", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert float(probe(out)["format"]["duration"]) == pytest.approx(1.1, abs=0.05)
    r = runner.invoke(main, ["--db", str(dbp), "video", str(camera_bag), "--topic", "/nope"])
    assert r.exit_code != 0 and "--list" in r.output


def test_view_order_and_grid():
    names = ["/cam/rear", "/sensing/camera/front_right/image", "/sensing/camera/center/image",
             "/sensing/camera/front_left/image", "/misc"]
    assert video.view_order(names) == ["/sensing/camera/front_left/image", "/sensing/camera/center/image",
                                       "/sensing/camera/front_right/image", "/misc", "/cam/rear"]
    assert [video.grid_for(n) for n in (1, 2, 3, 4, 5, 6, 7, 9, 10)] == [
        (1, 1), (1, 2), (1, 3), (2, 2), (2, 3), (2, 3), (3, 3), (3, 3), (3, 4)]


@needs_ffmpeg
def test_tile_videos_with_different_frame_rates(conn, camera_bag, tmp_path):
    catalog.scan(conn, [camera_bag])
    bag = catalog.get_bag(conn, camera_bag)
    cams = video.camera_topics(catalog.topics_of(conn, bag["id"]))  # 10 fps JPEG + 2 fps raw
    progress = []
    out = video.make_tiled_video(camera_bag, cams, tmp_path / "tiled.mp4", bag["start_ns"], bag["end_ns"],
                                 on_progress=progress.append)
    info = probe(out)
    stream = info["streams"][0]
    assert stream["r_frame_rate"] == "10/1"
    assert float(info["format"]["duration"]) == pytest.approx(3.1, abs=0.15)  # no runaway frame duplication
    assert stream["height"] == 480 and stream["width"] > 2 * 480
    assert progress[-1] == 1.0
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []  # no leftover .part files

    four = video.tile_videos([out] * 4, list("abcd"), tmp_path / "grid.mp4", bag["start_ns"])
    assert (probe(four)["streams"][0]["height"]) == 720  # 2 x 2 at 360 px


@needs_ffmpeg
def test_web_and_cli_tiled(tmp_path, camera_bag, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    dbp = tmp_path / "c.db"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(dbp), "scan", str(camera_bag)])
    client = TestClient(create_app(dbp))

    page = client.get("/bags/1").text
    assert 'data-topic="*"' in page and "all (2)" in page and 'class="mosaic"' in page
    client.post("/bags/1/video", data={"topic": "*"})
    job = wait_for_job(client)
    assert job["state"] == "completed" and job["label"].startswith("Preview all cameras"), job
    assert client.get("/bags/1/video", params={"topic": "*"}).headers["content-type"] == "video/mp4"
    # the single-camera previews made on the way are reused
    assert client.get("/bags/1/video", params={"topic": "/cam/raw"}).status_code == 200
    assert client.get("/bags/1/thumbnail", params={"topic": "*"}).status_code == 404

    out = tmp_path / "tiled.mp4"
    r = runner.invoke(main, ["--db", str(dbp), "video", str(camera_bag), "--tile", "-s", "0.5", "-e", "1.5",
                             "--height", "120", "-o", str(out)])
    assert r.exit_code == 0, r.output
    info = probe(out)
    assert float(info["format"]["duration"]) == pytest.approx(1.1, abs=0.15)
    r = runner.invoke(main, ["--db", str(dbp), "video", str(camera_bag), "--topic", "/cam/raw",
                             "--topic", "/sensing/camera/front/image_raw/compressed"])
    assert r.exit_code != 0 and "--tile" in r.output


def test_job_lanes_run_independently():
    import threading

    from bagarium.web.jobs import JobQueue

    q = JobQueue({"main": 1, "video": 2})
    gate, running = threading.Event(), []

    def slow(j):
        running.append(j.id)
        gate.wait(5)

    main_job = q.submit("scan", "slow scan", slow)
    v1 = q.submit("video", "v1", slow, lane="video")
    v2 = q.submit("video", "v2", slow, lane="video")
    v3 = q.submit("video", "v3", slow, lane="video")
    import time
    deadline = time.monotonic() + 5
    while len(running) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    states = {j["id"]: j["state"] for j in q.list()}
    # the main lane's job does not hold up videos; two videos run at once, the third waits
    assert states[main_job] == "running" and states[v1] == states[v2] == "running" and states[v3] == "queued"
    gate.set()
    assert q.wait_idle()
    with pytest.raises(ValueError):
        q.submit("x", "x", slow, lane="nope")


@needs_ffmpeg
def test_batch_previews_web_and_cli(tmp_path, camera_bag, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    second = tmp_path / "copy" / "cams.db3"  # same name in another folder
    second.parent.mkdir()
    second.write_bytes(camera_bag.read_bytes())
    dbp = tmp_path / "c.db"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(dbp), "scan", str(camera_bag), str(second.parent)])
    client = TestClient(create_app(dbp))
    ids = [b["id"] for b in client.get("/api/bags").json()]
    assert 'formaction="/videos"' in client.get("/").text

    page = client.get("/videos", params={"bag": ids}).text
    assert "Preview videos for 2 bags" in page and "front" in page and "(2 bags)" in page
    r = client.post("/videos", data={"bag": ids, "what": "each"})
    assert r.url.path == "/jobs"
    wait_for_job(client)
    jobs = client.get("/api/jobs").json()
    assert len(jobs) == 4 and {j["state"] for j in jobs} == {"completed"} and {j["lane"] for j in jobs} == {"video"}
    assert "nothing to do" in client.post("/videos", data={"bag": ids, "what": "each"}).text
    client.post("/videos", data={"bag": ids, "what": "*"})
    wait_for_job(client)
    assert len(client.get("/api/jobs").json()) == 6

    out = tmp_path / "videos"
    r = runner.invoke(main, ["--db", str(dbp), "video", str(camera_bag), str(second), "--tile", "-O", str(out),
                             "-j", "2", "--height", "96"])
    assert r.exit_code == 0, r.output
    assert sorted(p.name for p in out.iterdir()) == ["cams_tiled.mp4", "cams_tiled_2.mp4"]
    assert "wrote 2, skipped 0, failed 0" in r.output


def test_zip_stream_roundtrip(tmp_path):
    import io
    import os
    import zipfile

    big, small = tmp_path / "big.bin", tmp_path / "small.bin"
    big.write_bytes(os.urandom(2_500_000))
    small.write_bytes(b"x")
    chunks = list(video.zip_stream([(big, "a/big.mp4"), (small, "small.mp4")], chunk=1 << 18))
    assert len(chunks) > 5  # streamed in pieces, not built in one go
    archive = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
    assert archive.testzip() is None and archive.namelist() == ["a/big.mp4", "small.mp4"]
    assert archive.read("a/big.mp4") == big.read_bytes()
    assert {i.compress_type for i in archive.infolist()} == {zipfile.ZIP_STORED}


@needs_ffmpeg
def test_download_previews(tmp_path, camera_bag, monkeypatch):
    import io
    import zipfile

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    second = tmp_path / "copy" / "cams.db3"
    second.parent.mkdir()
    second.write_bytes(camera_bag.read_bytes())
    dbp = tmp_path / "c.db"
    CliRunner().invoke(main, ["--db", str(dbp), "scan", str(camera_bag), str(second.parent)])
    client = TestClient(create_app(dbp))
    ids = [b["id"] for b in client.get("/api/bags").json()]

    page = client.get("/videos", params={"bag": ids}).text
    assert "(0 ready)" in page and "Download zip" in page
    r = client.get("/videos/download", params={"bag": ids})
    assert "none of the selected previews" in r.text

    client.post("/videos", data={"bag": ids, "what": "*"})  # tiled, which also makes each camera's preview
    wait_for_job(client)
    assert "(6 ready)" in client.get("/videos", params={"bag": ids}).text

    def names(**params):
        r = client.get("/videos/download", params={"bag": ids, **params})
        assert r.headers["content-type"] == "application/zip"
        assert r.headers["content-disposition"].startswith('attachment; filename="bagarium-previews-')
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        assert archive.testzip() is None
        return sorted(archive.namelist())

    assert names(what="*") == ["cams_all.mp4", "cams_all_2.mp4"]  # same bag name twice
    assert names(what="/cam/raw") == ["cams_cam_raw.mp4", "cams_cam_raw_2.mp4"]
    assert len(names(what="each")) == 4 and len(names()) == 6

    single = client.get("/bags/1/video", params={"topic": "*", "download": 1})
    assert 'filename="cams_all.mp4"' in single.headers["content-disposition"]
    assert "Download this video" in client.get("/bags/1").text
