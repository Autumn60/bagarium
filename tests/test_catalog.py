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


def test_roots(conn, bag_root):
    catalog.add_roots(conn, [bag_root, bag_root])
    with pytest.raises(ValueError):
        catalog.add_roots(conn, [bag_root / "nope"])
    [root] = catalog.list_roots(conn)
    assert root["path"] == str(bag_root) and root["bag_count"] == 0 and root["last_scanned_at"] is None

    catalog.scan(conn, [bag_root])
    [root] = catalog.list_roots(conn)
    assert root["bag_count"] == 4 and root["last_scanned_at"]

    assert catalog.remove_roots(conn, [bag_root]) == 1
    assert catalog.list_roots(conn) == [] and len(catalog.search(conn)) == 4


def test_overlapping_roots_index_each_bag_once(conn, bag_root):
    seen = []
    r = catalog.scan(conn, [bag_root, bag_root / "loose"], on_bag=lambda p, s, d, t: seen.append((p, d, t)))
    assert r.added == 4 and len(seen) == 4 and seen[-1][1:] == (4, 4)


def test_abort_keeps_partial_progress(conn, bag_root):
    def stop_after_two(path, status, done, total):
        if done == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        catalog.scan(conn, [bag_root], on_bag=stop_after_two)
    assert len(catalog.search(conn, include_missing=True)) == 2


def test_search_by_root(conn, bag_root, tmp_path):
    from conftest import make_mcap

    sibling = tmp_path / "bags2"  # shares the "bags" prefix but is not under it
    make_mcap(sibling / "other.mcap", {"/a": ("std_msgs/msg/String", 1)})
    catalog.scan(conn, [bag_root, sibling])

    assert len(catalog.search(conn, root=bag_root)) == 4
    assert names(catalog.search(conn, root=bag_root / "loose")) == ["camera.mcap", "old.db3"]
    assert names(catalog.search(conn, root=sibling)) == ["other.mcap"]
    assert names(catalog.search(conn, root=bag_root / "loose" / "camera.mcap")) == ["camera.mcap"]
    assert names(catalog.search(conn, "old", root=bag_root)) == ["old.db3"]


def test_parallel_scan_matches_sequential(conn, bag_root, tmp_path, monkeypatch):
    (bag_root / "broken.mcap").write_bytes(b"garbage")
    monkeypatch.setattr(catalog, "PARALLEL_MIN", 1)
    seen = []
    r = catalog.scan(conn, [bag_root], workers=2, on_bag=lambda p, s, d, t: seen.append((s, d, t)))
    assert (r.added, len(r.errors)) == (5, 1)
    assert sorted(s for s, _, _ in seen) == ["added"] * 4 + ["error"]
    assert [d for _, d, _ in seen] == [1, 2, 3, 4, 5] and {t for _, _, t in seen} == {5}

    from bagarium import db
    seq = db.connect(tmp_path / "seq.db")
    catalog.scan(seq, [bag_root], workers=1)
    fields = "name, storage, start_ns, end_ns, message_count, size_bytes, error IS NULL"
    q = f"SELECT {fields} FROM bags ORDER BY path"
    assert [tuple(r) for r in conn.execute(q)] == [tuple(r) for r in seq.execute(q)]
    tq = "SELECT b.name, t.name, t.type, t.message_count FROM topics t JOIN bags b ON b.id = t.bag_id ORDER BY 1, 2"
    assert [tuple(r) for r in conn.execute(tq)] == [tuple(r) for r in seq.execute(tq)]


def test_migrates_old_topics_table(tmp_path):
    import sqlite3

    from bagarium import db

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE bags (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL, storage TEXT NOT NULL,
            start_ns INTEGER, end_ns INTEGER, message_count INTEGER NOT NULL DEFAULT 0, size_bytes INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', missing INTEGER NOT NULL DEFAULT 0, error TEXT,
            indexed_at TEXT NOT NULL);
        CREATE TABLE topics (bag_id INTEGER NOT NULL REFERENCES bags(id) ON DELETE CASCADE, name TEXT NOT NULL,
            type TEXT NOT NULL, message_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bag_id, name, type));
        INSERT INTO bags (id, path, name, storage, indexed_at) VALUES (1, '/a', 'a', 'mcap', ''), (2, '/b', 'b', 'mcap', '');
        INSERT INTO topics VALUES (1, '/tf', 'tf2_msgs/msg/TFMessage', 10), (2, '/tf', 'tf2_msgs/msg/TFMessage', 20),
                                  (2, '/odom', 'nav_msgs/msg/Odometry', 5);
    """)
    old.commit()
    old.close()

    conn = db.connect(path)
    assert conn.execute("SELECT type FROM sqlite_master WHERE name = 'topics'").fetchone()[0] == "view"
    assert conn.execute("SELECT COUNT(*) FROM topic_names").fetchone()[0] == 2
    assert [tuple(r) for r in catalog.topics_of(conn, 2)] == [("/odom", "nav_msgs/msg/Odometry", 5),
                                                             ("/tf", "tf2_msgs/msg/TFMessage", 20)]
    assert names(catalog.search(conn, topics=["/odom"])) == ["b"]
    conn.close()
    db.connect(path).close()  # connecting again is a no-op
