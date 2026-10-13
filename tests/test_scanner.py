from bagarium import scanner

from conftest import T0, make_mcap


def test_discover(bag_root):
    found = sorted(p.relative_to(bag_root).as_posix() for p in scanner.discover(bag_root))
    assert found == ["2025-10-09/run_db3", "2025-10-09/run_lidar", "loose/camera.mcap", "loose/old.db3"]


def test_discover_single_bag_dir(bag_root):
    bag = bag_root / "2025-10-09" / "run_lidar"
    assert list(scanner.discover(bag)) == [bag]


def test_read_metadata_dir(bag_root):
    info = scanner.read_bag(bag_root / "2025-10-09" / "run_lidar")
    assert info.storage == "mcap"
    assert info.message_count == 32
    assert info.start_ns == T0
    assert info.duration_ns == 2_000_000_000
    assert {t.name: t.message_count for t in info.topics} == {"/lidar/points": 11, "/tf": 21}
    assert info.size_bytes > 0


def test_read_mcap(bag_root):
    info = scanner.read_bag(bag_root / "loose" / "camera.mcap")
    assert info.storage == "mcap"
    assert info.message_count == 60
    assert info.start_ns == T0 + 86400 * 10**9
    assert info.duration_ns == 2_900_000_000
    assert {(t.name, t.type) for t in info.topics} == {
        ("/camera/front/image_raw", "sensor_msgs/msg/Image"),
        ("/camera/rear/image_raw", "sensor_msgs/msg/Image"),
    }


def test_read_mcap_without_summary(tmp_path):
    path = make_mcap(tmp_path / "cut.mcap", {"/a": ("std_msgs/msg/String", 7)}, finish=False)
    info = scanner.read_bag(path)
    assert info.message_count == 7
    assert info.duration_ns == 600_000_000


def test_read_db3(bag_root):
    info = scanner.read_bag(bag_root / "loose" / "old.db3")
    assert info.storage == "sqlite3"
    assert info.message_count == 50
    assert info.topics[0].name == "/odom"
