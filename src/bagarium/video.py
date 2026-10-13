"""Camera previews (thumbnails and MP4) from image topics, without ROS. Needs ffmpeg for video.

- ``sensor_msgs/msg/CompressedImage`` (JPEG / PNG): frames are passed to ffmpeg as they are
- ``sensor_msgs/msg/Image`` (rgb8, bgr8, rgba8, bgra8, mono8, mono16, bayer_*8): raw frames

Videos run on the bag's clock: time t in the video is t seconds after the bag
start (gaps are filled by repeating the previous frame), so a position in the
video maps directly to a position on the track.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import math
import os
import tempfile
import shutil
import sqlite3
import struct
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import bagreader

COMPRESSED = "sensor_msgs/msg/CompressedImage"
RAW = "sensor_msgs/msg/Image"
MAX_FPS = 15.0
FPS_PROBE_FRAMES = 30
PREVIEW_HEIGHT = 720

RAW_PIX_FMTS = {
    "rgb8": "rgb24", "bgr8": "bgr24", "rgba8": "rgba", "bgra8": "bgra", "mono8": "gray", "8UC1": "gray",
    "mono16": "gray16", "16UC1": "gray16",
    "bayer_rggb8": "bayer_rggb8", "bayer_bggr8": "bayer_bggr8",
    "bayer_gbrg8": "bayer_gbrg8", "bayer_grbg8": "bayer_grbg8",
}


class VideoError(Exception):
    pass


def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise VideoError("ffmpeg is not installed (e.g. `sudo apt install ffmpeg`)")
    return path


# ---------------------------------------------------------------- topics


def camera_topics(topics: Iterable[sqlite3.Row | dict]) -> list[dict]:
    """Image topics of a bag that can be previewed (depth streams and empty topics are left out)."""
    found = [{"name": t["name"], "type": t["type"], "message_count": t["message_count"]}
             for t in topics
             if t["type"] in (COMPRESSED, RAW) and t["message_count"] and "depth" not in t["name"].lower()]
    return sorted(found, key=lambda t: t["name"])


_NOISE = {"sensing", "camera", "cameras", "image_raw", "image", "image_rect", "image_color", "compressed"}


def short_name(topic: str) -> str:
    """A short label for a camera topic: /sensing/camera/center/image_raw/compressed -> center."""
    parts = [p for p in topic.strip("/").split("/") if p and p not in _NOISE]
    return "/".join(parts) or topic


def view_order(topics: Iterable[str]) -> list[str]:
    """Order camera topics as seen from the driver's seat: left, centre/front, right, then the rest."""
    def rank(topic: str) -> tuple[int, str]:
        name = topic.lower()
        if "left" in name:
            pos = 0
        elif "right" in name:
            pos = 2
        elif "center" in name or "centre" in name or "front" in name:
            pos = 1
        else:
            pos = 3
        return (1 if "rear" in name or "back" in name else 0, pos, topic)

    return sorted(topics, key=rank)


def grid_for(n: int) -> tuple[int, int]:
    """(rows, cols) for n tiles: one row up to 3, then a near-square grid."""
    if n <= 3:
        return 1, n
    cols = math.ceil(math.sqrt(n))
    return math.ceil(n / cols), cols


# ---------------------------------------------------------------- CDR decoding


@dataclass
class Frame:
    kind: str           # "jpeg", "png" or "raw"
    data: bytes
    width: int = 0      # raw frames only
    height: int = 0
    pix_fmt: str = ""


def _read_string(data: bytes, order: str, o: int) -> tuple[str, int]:
    o = 4 + (o - 4 + 3) // 4 * 4
    (n,) = struct.unpack_from(order + "I", data, o)
    return data[o + 4:o + 4 + n - 1].decode(errors="replace"), o + 4 + n


def _read_bytes(data: bytes, order: str, o: int) -> bytes:
    o = 4 + (o - 4 + 3) // 4 * 4
    (n,) = struct.unpack_from(order + "I", data, o)
    return data[o + 4:o + 4 + n]


def decode_frame(type_: str, data: bytes) -> Frame:
    if len(data) < 16 or data[0] != 0:
        raise ValueError("not a CDR message")
    order = "<" if data[1] & 1 else ">"
    _, o = _read_string(data, order, 12)  # header: stamp (8 bytes after encapsulation) + frame_id
    if type_ == COMPRESSED:
        fmt, o = _read_string(data, order, o)
        payload = _read_bytes(data, order, o)
        if payload[:2] == b"\xff\xd8":
            return Frame("jpeg", payload)
        if payload[:4] == b"\x89PNG":
            return Frame("png", payload)
        raise ValueError(f"unsupported compressed image format: {fmt}")
    if type_ == RAW:
        o = 4 + (o - 4 + 3) // 4 * 4
        height, width = struct.unpack_from(order + "II", data, o)
        encoding, o = _read_string(data, order, o + 8)
        big_endian = data[o]
        o = 4 + (o + 1 - 4 + 3) // 4 * 4  # step (uint32) after is_bigendian (uint8)
        payload = _read_bytes(data, order, o + 4)
        pix_fmt = RAW_PIX_FMTS.get(encoding)
        if pix_fmt is None:
            raise ValueError(f"unsupported image encoding: {encoding}")
        if pix_fmt == "gray16":
            pix_fmt = "gray16be" if big_endian else "gray16le"
        return Frame("raw", payload, width, height, pix_fmt)
    raise ValueError(f"not an image type: {type_}")


# ---------------------------------------------------------------- thumbnails


def thumbnail(path: str | Path, topic: str, type_: str, at_ns: int | None = None) -> tuple[bytes, str]:
    """(image bytes, media type) of the first frame at or after ``at_ns``."""
    for _, _, data in bagreader.iter_messages(path, [topic], start_ns=at_ns):
        frame = decode_frame(type_, data)
        if frame.kind == "jpeg":
            return frame.data, "image/jpeg"
        if frame.kind == "png":
            return frame.data, "image/png"
        return _raw_to_jpeg(frame), "image/jpeg"
    raise VideoError(f"no frames on {topic}")


def _raw_to_jpeg(frame: Frame) -> bytes:
    cmd = [ffmpeg_path(), "-v", "error", "-f", "rawvideo", "-pix_fmt", frame.pix_fmt,
           "-s", f"{frame.width}x{frame.height}", "-i", "-", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-"]
    return subprocess.run(cmd, input=frame.data, capture_output=True, check=True).stdout


# ---------------------------------------------------------------- video


def make_video(
    path: str | Path,
    topic: str,
    type_: str,
    out: str | Path,
    bag_start_ns: int,
    bag_end_ns: int,
    frame_count: int,
    start_ns: int | None = None,
    end_ns: int | None = None,
    max_height: int | None = PREVIEW_HEIGHT,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Encode ``topic`` to an H.264 MP4 at ``out`` on the bag's clock.

    Without ``start_ns`` the video starts at the bag start, so time t in the
    video is t seconds into the bag. The frame rate is derived from
    ``frame_count`` (messages on the topic) over the bag duration.
    ``on_progress(fraction)`` may raise to abort; nothing is left at ``out`` then.
    """
    out = Path(out)
    origin = start_ns if start_ns is not None else bag_start_ns
    finish = end_ns if end_ns is not None else bag_end_ns
    messages = bagreader.iter_messages(path, [topic], start_ns, end_ns)
    # Look at the first frames to find the camera's rate (an average over the
    # whole bag would be lowered by gaps).
    head = list(itertools.islice(messages, FPS_PROBE_FRAMES))
    duration = max((bag_end_ns - bag_start_ns) / 1e9, 1e-3)
    fps = _estimate_fps([t for _, t, _ in head]) or min(MAX_FPS, max(1.0, round(frame_count / duration)))
    total_frames = max(1, round((finish - origin) / 1e9 * fps) + 1)

    proc = None
    tmp = out.with_name(f".{out.name}.part")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        written = 0
        previous: bytes | None = None
        for _, t, data in itertools.chain(head, messages):
            frame = decode_frame(type_, data)
            if proc is None:
                proc = _start_encoder(frame, fps, max_height, tmp)
            index = round((t - origin) / 1e9 * fps)
            # Repeat frames to stay on the bag's clock (the first frame also fills
            # the lead-in); frames arriving faster than fps are dropped.
            while written < index:
                proc.stdin.write(previous if previous is not None else frame.data)
                written += 1
            if written == index:
                proc.stdin.write(frame.data)
                written += 1
            previous = frame.data
            if on_progress:
                on_progress(min(written / total_frames, 0.99))
        if proc is None:
            raise VideoError(f"no frames on {topic} in the selected range")
        while written < total_frames:  # hold the last frame until the end
            proc.stdin.write(previous)
            written += 1
        proc.stdin.close()
        if proc.wait() != 0:
            raise VideoError("ffmpeg failed: " + proc.stderr.read().decode(errors="replace")[-500:])
        os.replace(tmp, out)
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if proc is not None:
            proc.stderr.close()
    if on_progress:
        on_progress(1.0)
    return out


def _estimate_fps(stamps: list[int]) -> float | None:
    gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]) if b > a)
    if not gaps:
        return None
    median = gaps[len(gaps) // 2] / 1e9
    return float(min(MAX_FPS, max(1, round(1 / median))))


def _start_encoder(frame: Frame, fps: float, max_height: int | None, out: Path) -> subprocess.Popen:
    if frame.kind == "raw":
        source = ["-f", "rawvideo", "-pix_fmt", frame.pix_fmt, "-s", f"{frame.width}x{frame.height}"]
    else:
        source = ["-f", "image2pipe", "-c:v", "mjpeg" if frame.kind == "jpeg" else "png"]
    scale = f"scale=-2:'min({max_height},ih)'," if max_height else ""
    cmd = [ffmpeg_path(), "-v", "error", "-y", *source, "-framerate", str(fps), "-i", "-",
           "-vf", f"{scale}pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-movflags", "+faststart",
           "-f", "mp4", str(out)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# ---------------------------------------------------------------- tiling


def _probe(path: Path) -> tuple[int, int, float, float]:
    """(width, height, duration in s, frame rate) of a video."""
    ffprobe = shutil.which("ffprobe") or str(Path(ffmpeg_path()).with_name("ffprobe"))
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate:format=duration", "-of", "default=nw=1", str(path)],
                         capture_output=True, text=True, check=True).stdout
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    num, _, den = values.get("r_frame_rate", "0/1").partition("/")
    rate = float(num) / float(den or 1) if float(den or 1) else 0.0
    return int(values["width"]), int(values["height"]), float(values.get("duration") or 0), rate


def tile_videos(
    inputs: list[Path],
    labels: list[str],
    out: str | Path,
    clock_origin_ns: int | None = None,
    cell_height: int | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Put videos that share a clock side by side (see ``grid_for``) into one MP4.

    Each tile is letterboxed to the first video's aspect ratio and labelled;
    with ``clock_origin_ns`` (the epoch time of t=0) the local time is shown on top.
    ``on_progress(fraction)`` may raise to abort; nothing is left at ``out`` then.
    """
    out = Path(out)
    rows, cols = grid_for(len(inputs))
    probes = [_probe(p) for p in inputs]
    width, height = probes[0][0], probes[0][1]
    # Every input covers the same span; they differ only in how long their last frame lasts
    # (1/fps), so the shortest one is the span without trailing hold.
    duration = min(p[2] for p in probes) or 1.0
    # Inputs at different rates must be brought to one rate before stacking,
    # or the output frame rate is undefined and ffmpeg duplicates frames endlessly.
    rate = min(MAX_FPS, max(p[3] for p in probes)) or 10.0
    cell_h = cell_height or (480 if rows == 1 else 360)
    cell_w = round(cell_h * width / height / 2) * 2
    font = max(12, cell_h // 20)
    box = "fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=6"

    with tempfile.TemporaryDirectory(prefix="bagarium-tile-") as tmp:
        parts, layout = [], []
        for i, label in enumerate(labels):
            label_file = Path(tmp) / f"label{i}.txt"
            label_file.write_text(label)
            parts.append(
                f"[{i}:v]scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
                f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate:g},"
                f"drawtext=font=Sans:textfile='{label_file}':x=10:y=h-th-10:fontsize={font}:{box}[v{i}]"
            )
            layout.append(f"{(i % cols) * cell_w}_{(i // cols) * cell_h}")
        for i in range(len(inputs), rows * cols):  # blank cells keep the grid rectangular
            parts.append(f"color=c=black:s={cell_w}x{cell_h}:r={rate:g}:d={duration:.3f}[v{i}]")
            layout.append(f"{(i % cols) * cell_w}_{(i // cols) * cell_h}")
        stacked = "".join(f"[v{i}]" for i in range(rows * cols))
        graph = ";".join(parts) + f";{stacked}xstack=inputs={rows * cols}:layout={'|'.join(layout)}:fill=black"
        if clock_origin_ns is not None:
            origin = f"{clock_origin_ns / 1e9:.3f}"
            graph += (f",drawtext=font=Sans:text='%{{pts\\:localtime\\:{origin}\\:%Y-%m-%d %H\\\\\\:%M\\\\\\:%S}}'"
                      f":x=(w-tw)/2:y=10:fontsize={font}:{box}")
        graph += ",format=yuv420p"
        script = Path(tmp) / "graph.txt"
        script.write_text(graph)

        tmp_out = out.with_name(f".{out.name}.part")
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [ffmpeg_path(), "-v", "error", "-y", *[a for p in inputs for a in ("-i", str(p))],
               "-filter_complex_script", str(script), "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
               "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-f", "mp4", str(tmp_out)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for line in proc.stdout:
                if line.startswith("out_time_us=") and on_progress:
                    us = line.split("=", 1)[1].strip()
                    if us.isdigit():
                        on_progress(min(int(us) / 1e6 / duration, 0.99))
            if proc.wait() != 0:
                raise VideoError("ffmpeg failed: " + proc.stderr.read()[-500:])
            os.replace(tmp_out, out)
        except BaseException:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            tmp_out.unlink(missing_ok=True)
            raise
        finally:
            proc.stdout.close()
            proc.stderr.close()
    if on_progress:
        on_progress(1.0)
    return out


def make_tiled_video(
    path: str | Path,
    cams: list[dict],
    out: str | Path,
    bag_start_ns: int,
    bag_end_ns: int,
    start_ns: int | None = None,
    end_ns: int | None = None,
    max_height: int | None = PREVIEW_HEIGHT,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Encode several camera topics (``{"name", "type", "message_count"}``) side by side, in the given order."""
    with tempfile.TemporaryDirectory(prefix="bagarium-tile-") as tmp:
        inputs = []
        steps = len(cams) + 1
        for k, cam in enumerate(cams):
            inputs.append(make_video(
                path, cam["name"], cam["type"], Path(tmp) / f"{k}.mp4", bag_start_ns, bag_end_ns,
                cam["message_count"], start_ns, end_ns, max_height,
                (lambda f, k=k: on_progress((k + f) / steps)) if on_progress else None,
            ))
        origin = start_ns if start_ns is not None else bag_start_ns
        return tile_videos(inputs, [short_name(c["name"]) for c in cams], out, origin,
                           on_progress=(lambda f: on_progress((len(cams) + f) / steps)) if on_progress else None)


# ---------------------------------------------------------------- cache


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "bagarium"


ALL_CAMERAS = "*"  # pseudo topic for the tiled preview of every camera


def _names(bag: sqlite3.Row | dict, topic: str) -> tuple[str, str]:
    """(prefix shared by all versions of this bag's topic, version of the bag's current files)."""
    topic_key = hashlib.sha1(topic.encode()).hexdigest()[:12]
    version = hashlib.sha1(repr(bag["mtime"]).encode()).hexdigest()[:8]
    return f"{bag['id']}-{topic_key}", version


def preview_path(bag: sqlite3.Row | dict, topic: str) -> Path:
    """Cached preview video of a bag's camera topic (may not exist yet)."""
    prefix, version = _names(bag, topic)
    return cache_dir() / "videos" / f"{prefix}-{version}.mp4"


def thumbnail_path(bag: sqlite3.Row | dict, topic: str) -> Path:
    prefix, version = _names(bag, topic)
    return cache_dir() / "thumbnails" / f"{prefix}-{version}"


def cached_thumbnail(bag: sqlite3.Row | dict, topic: str, type_: str) -> tuple[bytes, str]:
    """Thumbnail from the middle of the bag, cached on disk."""
    base = thumbnail_path(bag, topic)
    for ext, media in ((".jpg", "image/jpeg"), (".png", "image/png")):
        if base.with_suffix(ext).exists():
            return base.with_suffix(ext).read_bytes(), media
    middle = None
    if bag["start_ns"] is not None and bag["end_ns"] is not None:
        middle = (bag["start_ns"] + bag["end_ns"]) // 2
    try:
        data, media = thumbnail(bag["path"], topic, type_, middle)
    except VideoError:
        data, media = thumbnail(bag["path"], topic, type_)  # no frame after the middle
    _remove_stale(base.parent, bag, topic, keep=None)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".jpg" if media == "image/jpeg" else ".png").write_bytes(data)
    return data, media


def make_preview(bag: sqlite3.Row | dict, topic: str, type_: str, frame_count: int,
                 on_progress: Callable[[float], None] | None = None) -> Path:
    out = preview_path(bag, topic)
    if not out.exists():
        _remove_stale(out.parent, bag, topic, keep=out)
        make_video(bag["path"], topic, type_, out, bag["start_ns"], bag["end_ns"], frame_count,
                   on_progress=on_progress)
    return out


def _remove_stale(directory: Path, bag, topic: str, keep: Path | None) -> None:
    """Delete cached files of this bag's topic made for earlier versions of the bag."""
    prefix, _ = _names(bag, topic)
    for old in directory.glob(f"{prefix}-*"):
        if old != keep:
            old.unlink(missing_ok=True)


def make_tiled_preview(bag: sqlite3.Row | dict, cams: list[dict],
                       on_progress: Callable[[float], None] | None = None) -> Path:
    """Cached tiled preview of ``cams`` (in view order), reusing or making each camera's preview."""
    out = preview_path(bag, ALL_CAMERAS)
    if out.exists():
        return out
    cams = sorted(cams, key=lambda c: view_order([c["name"] for c in cams]).index(c["name"]))
    steps = len(cams) + 1
    inputs = []
    for k, cam in enumerate(cams):
        inputs.append(make_preview(bag, cam["name"], cam["type"], cam["message_count"],
                                   (lambda f, k=k: on_progress((k + f) / steps)) if on_progress else None))
    _remove_stale(out.parent, bag, ALL_CAMERAS, keep=out)
    return tile_videos(inputs, [short_name(c["name"]) for c in cams], out, bag["start_ns"],
                       on_progress=(lambda f: on_progress((len(cams) + f) / steps)) if on_progress else None)


# ---------------------------------------------------------------- downloads


def download_name(bag: sqlite3.Row | dict, topic: str) -> str:
    """A readable file name for a bag's preview: <bag>_<camera>.mp4 (or _all for the tiled one)."""
    stem = Path(bag["path"]).stem if Path(bag["path"]).suffix in (".mcap", ".db3") else bag["name"]
    camera = "all" if topic == ALL_CAMERAS else short_name(topic).replace("/", "_")
    return f"{stem}_{camera}.mp4"


class _Sink(io.RawIOBase):
    """Write-only stream that hands out what was written so far (for streaming a zip)."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def take(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def zip_stream(files: Iterable[tuple[Path, str]], chunk: int = 1 << 20) -> Iterator[bytes]:
    """Yield a zip archive of ``(path, name in archive)`` piece by piece.

    Videos are already compressed, so they are stored as they are; nothing is
    written to disk and the download starts immediately.
    """
    sink = _Sink()
    archive = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
    for path, name in files:
        info = zipfile.ZipInfo.from_file(path, name)
        info.compress_type = zipfile.ZIP_STORED
        with open(path, "rb") as src, archive.open(info, "w", force_zip64=True) as dst:
            while data := src.read(chunk):
                dst.write(data)
                yield sink.take()
    archive.close()
    yield sink.take()
