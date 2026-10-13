import shutil
import sqlite3

import pytest
import yaml
import zstandard
from click.testing import CliRunner

from bagarium import catalog, extract, scanner
from bagarium.cli import main

from conftest import T0, make_bag_dir, make_db3, make_mcap

S = 1_000_000_000


def read_messages(bag_dir):
    """(topic, timestamp) of every message in an output bag, via its own metadata."""
    meta = yaml.safe_load((bag_dir / "metadata.yaml").read_text())["rosbag2_bagfile_information"]
    [data] = meta["relative_file_paths"]
    path = bag_dir / data
    if meta["storage_identifier"] == "sqlite3":
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT t.name, m.timestamp FROM messages m JOIN topics t ON t.id = m.topic_id ORDER BY m.timestamp, t.name"
        ).fetchall()
        assert conn.execute("SELECT schema_version FROM schema").fetchone()[0] == 3
        return rows
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        return sorted((c.topic, m.log_time) for _, c, m in make_reader(f).iter_messages())


def test_parse_bag_time():
    assert extract.parse_bag_time("90", T0) == T0 + 90 * S
    assert extract.parse_bag_time("+1.5", T0) == T0 + 1_500_000_000
    assert extract.parse_bag_time("1:30", T0) == T0 + 90 * S
    assert extract.parse_bag_time("1:00:01", T0) == T0 + 3601 * S
    assert extract.parse_bag_time("2026-01-01T00:00:00", T0) == catalog.parse_time("2026-01-01T00:00:00")
    with pytest.raises(ValueError):
        extract.parse_bag_time("soon", T0)


def test_select_topics():
    names = ["/tf", "/tf_static", "/camera/front/image", "/camera/rear/image"]
    assert extract.select_topics(names, ["/tf"]) == ["/tf"]
    assert extract.select_topics(names, ["/camera/*"], ["*/rear/*"]) == ["/camera/front/image"]
    assert extract.select_topics(names, exclude=["/camera/*"]) == ["/tf", "/tf_static"]


@pytest.mark.parametrize("storage", ["sqlite3", "mcap"])
def test_extract_time_and_topics(tmp_path, storage):
    src = make_bag_dir(tmp_path / "src", {
        "/a": ("std_msgs/msg/String", 30),
        "/b": ("std_msgs/msg/Int32", 30),
    }, storage=storage)
    out = tmp_path / "out"
    result = extract.extract(src, out, T0 + 1 * S, T0 + 2 * S, include=["/a"])

    msgs = read_messages(out)
    assert {t for t, _ in msgs} == {"/a"} and len(msgs) == 11  # 1.0 .. 2.0 inclusive
    assert msgs[0][1] == T0 + S and msgs[-1][1] == T0 + 2 * S
    assert result.message_count == 11 and [t.name for t in result.topics] == ["/a"]

    info = scanner.read_bag(out)  # metadata.yaml is consistent with the data
    assert info.storage == storage and info.message_count == 11
    assert info.start_ns == T0 + S and info.duration_ns == S
    assert [(t.name, t.type, t.message_count) for t in info.topics] == [("/a", "std_msgs/msg/String", 11)]
    assert not list(tmp_path.glob(".bagarium-*"))


def test_extract_split_and_zstd_file_compressed(tmp_path):
    src = tmp_path / "split"
    src.mkdir()
    make_db3(src / "split_0.db3", {"/a": ("std_msgs/msg/String", 10)})
    make_db3(src / "split_1.db3", {"/a": ("std_msgs/msg/String", 10)}, start=T0 + 1 * S)
    for f in src.glob("*.db3"):
        with open(f, "rb") as fi, open(f.with_name(f.name + ".zstd"), "wb") as fo:
            zstandard.ZstdCompressor().copy_stream(fi, fo)
        f.unlink()
    (src / "metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
        "version": 5, "storage_identifier": "sqlite3",
        "duration": {"nanoseconds": 1_900_000_000}, "starting_time": {"nanoseconds_since_epoch": T0},
        "message_count": 20,
        "topics_with_message_count": [{"topic_metadata": {"name": "/a", "type": "std_msgs/msg/String",
                                                          "serialization_format": "cdr", "offered_qos_profiles": ""},
                                       "message_count": 20}],
        "compression_format": "zstd", "compression_mode": "FILE",
        "relative_file_paths": ["split_0.db3.zstd", "split_1.db3.zstd"],
    }}))

    extract.extract(src, tmp_path / "out", start_ns=T0 + 500_000_000)
    msgs = read_messages(tmp_path / "out")
    assert len(msgs) == 15 and msgs[-1][1] == T0 + 1_900_000_000
    assert sorted(p.name for p in src.iterdir()) == ["metadata.yaml", "split_0.db3.zstd", "split_1.db3.zstd"]


def test_extract_standalone_mcap_keeps_schema(tmp_path):
    src = make_mcap(tmp_path / "cam.mcap", {"/img": ("sensor_msgs/msg/Image", 5), "/x": ("std_msgs/msg/Empty", 5)})
    extract.extract(src, tmp_path / "out", include=["/img"])
    from mcap.reader import make_reader

    with open(tmp_path / "out" / "out_0.mcap", "rb") as f:
        summary = make_reader(f).get_summary()
    assert [s.name for s in summary.schemas.values()] == ["sensor_msgs/msg/Image"]
    assert summary.statistics.message_count == 5


def test_extract_errors_and_cancel(tmp_path):
    src = make_bag_dir(tmp_path / "src", {"/a": ("std_msgs/msg/String", 5000)}, storage="sqlite3")
    with pytest.raises(FileExistsError):
        extract.extract(src, src)
    with pytest.raises(ValueError, match="no topics"):
        extract.extract(src, tmp_path / "out", include=["/nope"])
    with pytest.raises(ValueError, match="after end"):
        extract.extract(src, tmp_path / "out", T0 + 2 * S, T0 + S)

    def cancel(frac, written):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        extract.extract(src, tmp_path / "out", on_progress=cancel)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src"]


def test_cli_extract(tmp_path, bag_root):
    dbp = tmp_path / "c.db"
    runner = CliRunner()
    assert runner.invoke(main, ["--db", str(dbp), "scan", str(bag_root)]).exit_code == 0
    bag = str(bag_root / "2025-10-09" / "run_lidar")
    out = tmp_path / "cut"

    r = runner.invoke(main, ["--db", str(dbp), "extract", bag, "-t", "/nope"])
    assert r.exit_code != 0 and "no topics match" in r.output

    r = runner.invoke(main, ["--db", str(dbp), "extract", bag, "-o", str(out), "-s", "1", "-d", "0.5", "-x", "/lidar/*"])
    assert r.exit_code == 0, r.output
    assert "wrote 6 messages on 1 topics" in r.output and "added to catalog" in r.output
    assert read_messages(out) == [("/tf", T0 + S + i * 100_000_000) for i in range(6)]

    r = runner.invoke(main, ["--db", str(dbp), "extract", bag, "-o", str(out)])
    assert r.exit_code != 0 and "already exists" in r.output


def test_nested_bags_are_discovered(tmp_path):
    parent = make_bag_dir(tmp_path / "run", {"/a": ("std_msgs/msg/String", 3)})
    child = make_bag_dir(parent / "derivatives" / "cut", {"/a": ("std_msgs/msg/String", 2)})
    assert list(scanner.discover(tmp_path)) == [parent, child]
    size, _ = scanner.stat_bag(parent)
    assert size == sum(p.stat().st_size for p in parent.iterdir() if p.is_file())
