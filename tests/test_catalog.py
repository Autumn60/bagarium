import shutil

import pytest

from bagarium import catalog

from conftest import T0


def names(rows):
    return sorted(r["name"] for r in rows)


def test_scan_and_rescan(conn, bag_root):
    r = catalog.scan(conn, [bag_root])
    assert (r.added, r.updated, r.unchanged, r.missing, r.errors) == (4, 0, 0, 0, [])
    r = catalog.scan(conn, [bag_root])
    assert (r.added, r.unchanged) == (0, 4)
    r = catalog.scan(conn, [bag_root], force=True)
    assert r.updated == 4


def test_missing_keeps_annotations_and_moved_is_relinked(conn, bag_root, tmp_path):
    catalog.scan(conn, [bag_root])
    bag = catalog.get_bag(conn, bag_root / "loose" / "camera.mcap")
    catalog.add_tags(conn, bag["id"], ["front"])
    catalog.set_note(conn, bag["id"], "sunny")

    moved_to = bag_root / "archive" / "camera_renamed.mcap"
    moved_to.parent.mkdir()
    shutil.move(bag_root / "loose" / "camera.mcap", moved_to)
    r = catalog.scan(conn, [bag_root])
    assert r.moved == 1 and r.added == 0

    row = catalog.get_bag(conn, bag["id"])
    assert row["path"] == str(moved_to) and row["note"] == "sunny" and not row["missing"]
    assert catalog.tags_of(conn, [bag["id"]])[bag["id"]] == ["front"]

    moved_to.unlink()
    r = catalog.scan(conn, [bag_root])
    assert r.missing == 1
    assert catalog.get_bag(conn, bag["id"])["missing"] == 1
    assert "camera_renamed.mcap" not in names(catalog.search(conn))
    assert "camera_renamed.mcap" in names(catalog.search(conn, include_missing=True))


def test_scan_subdir_does_not_mark_others_missing(conn, bag_root):
    catalog.scan(conn, [bag_root])
    r = catalog.scan(conn, [bag_root / "loose"])
    assert r.missing == 0


def test_corrupt_bag_is_listed_with_error(conn, tmp_path):
    (tmp_path / "broken.mcap").write_bytes(b"garbage")
    r = catalog.scan(conn, [tmp_path])
    assert len(r.errors) == 1
    row = catalog.get_bag(conn, tmp_path / "broken.mcap")
    assert row["error"] and row["storage"] == "mcap"


def test_search_filters(conn, bag_root):
    catalog.scan(conn, [bag_root])
    lidar = catalog.get_bag(conn, bag_root / "2025-10-09" / "run_lidar")
    catalog.add_tags(conn, lidar["id"], ["outdoor", "lidar"])
    catalog.set_note(conn, lidar["id"], "Rainy afternoon 100%")

    assert names(catalog.search(conn, tags=["outdoor"])) == ["run_lidar"]
    assert names(catalog.search(conn, tags=["outdoor", "nope"])) == []
    assert names(catalog.search(conn, "rainy")) == ["run_lidar"]
    assert names(catalog.search(conn, "100%")) == ["run_lidar"]
    assert names(catalog.search(conn, "loose")) == ["camera.mcap", "old.db3"]
    assert names(catalog.search(conn, topics=["/camera/*/image_raw"])) == ["camera.mcap"]
    assert names(catalog.search(conn, topics=["chatter"])) == ["run_db3"]
    assert names(catalog.search(conn, types=["PointCloud2"])) == ["run_lidar"]

    day = catalog.parse_time
    from datetime import datetime
    d0 = datetime.fromtimestamp(T0 / 1e9).date().isoformat()
    in_day = names(catalog.search(conn, since=day(d0), until=day(d0, end_of_day=True)))
    assert "run_lidar" in in_day and "old.db3" not in in_day and "camera.mcap" not in in_day

    order = [r["name"] for r in catalog.search(conn)]
    assert order[0] == "camera.mcap" and order[-1] == "old.db3"


def test_tags_lifecycle(conn, bag_root):
    catalog.scan(conn, [bag_root])
    bag_id = catalog.search(conn)[0]["id"]
    catalog.add_tags(conn, bag_id, ["  a  b ", "c", ""])
    assert catalog.tags_of(conn, [bag_id])[bag_id] == ["a b", "c"]
    catalog.remove_tags(conn, bag_id, ["c"])
    assert [t["name"] for t in catalog.all_tags(conn)] == ["a b"]
    catalog.forget(conn, [bag_id])
    assert catalog.all_tags(conn) == []


def test_parse_time_rejects_garbage():
    with pytest.raises(ValueError):
        catalog.parse_time("yesterday")
