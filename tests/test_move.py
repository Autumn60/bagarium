import errno
import os

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from bagarium import catalog, move
from bagarium.cli import main
from bagarium.web import create_app

from conftest import make_bag_dir
from test_cli_web import wait_for_job


@pytest.fixture
def indexed(conn, bag_root):
    catalog.scan(conn, [bag_root])
    return {r["name"]: dict(r) for r in catalog.search(conn)}


def test_plan_moves(conn, bag_root, indexed, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    lidar, cam = indexed["run_lidar"], indexed["camera.mcap"]

    [item] = move.plan_moves(conn, [lidar], dest)
    assert item.target == dest / "run_lidar" and item.status == "pending"
    [item] = move.plan_moves(conn, [lidar], dest / "renamed")
    assert item.target == dest / "renamed"
    [item] = move.plan_moves(conn, [cam], dest / "front")
    assert item.target == dest / "front.mcap"  # keeps the extension

    items = move.plan_moves(conn, [lidar, cam], tmp_path / "new_dir")  # several bags: always into the folder
    assert [i.target.name for i in items] == ["run_lidar", "camera.mcap"]

    (dest / "run_lidar").mkdir()
    assert "already exists" in move.plan_moves(conn, [lidar], dest)[0].message
    assert move.plan_moves(conn, [lidar], bag_root / "2025-10-09")[0].message == "already there"
    assert "into itself" in move.plan_moves(conn, [lidar], bag_root / "2025-10-09" / "run_lidar" / "x")[0].message
    assert "already going" in move.plan_moves(conn, [cam], dest, taken={dest / "camera.mcap"})[0].message

    twin = make_bag_dir(bag_root / "other" / "run_lidar", {"/a": ("std_msgs/msg/String", 1)})
    catalog.scan(conn, [bag_root])
    items = move.plan_moves(conn, [catalog.get_bag(conn, twin), indexed["run_db3"]], tmp_path / "d2")
    assert [i.status for i in items] == ["pending", "pending"]
    items = move.plan_moves(conn, [indexed["run_lidar"], catalog.get_bag(conn, twin)], tmp_path / "d3")
    assert [i.status for i in items] == ["pending", "skipped"]


def test_move_keeps_annotations_and_nested_bags(conn, bag_root, indexed, tmp_path):
    lidar = indexed["run_lidar"]
    nested = make_bag_dir(bag_root / "2025-10-09" / "run_lidar" / "derivatives" / "cut", {"/a": ("std_msgs/msg/String", 2)})
    catalog.scan(conn, [bag_root])
    nested_id = catalog.get_bag(conn, nested)["id"]
    catalog.add_tags(conn, lidar["id"], ["keep"])
    catalog.set_note(conn, lidar["id"], "note")

    target = bag_root / "archive" / "run_lidar"
    assert move.move_bag(conn, lidar["id"], target) == ""
    assert target.is_dir() and not (bag_root / "2025-10-09" / "run_lidar").exists()
    row = catalog.get_bag(conn, lidar["id"])
    assert row["path"] == str(target) and row["note"] == "note"
    assert catalog.tags_of(conn, [lidar["id"]])[lidar["id"]] == ["keep"]
    assert catalog.get_bag(conn, nested_id)["path"] == str(target / "derivatives" / "cut")

    r = catalog.scan(conn, [bag_root])
    assert (r.added, r.missing, r.moved) == (0, 0, 0)


def fake_cross_device(monkeypatch, sources):
    real = os.rename

    def rename(src, dst):
        if str(src) in sources:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst)

    monkeypatch.setattr(move.os, "rename", rename)


def test_move_across_filesystems(conn, bag_root, indexed, tmp_path, monkeypatch):
    lidar, cam = indexed["run_lidar"], indexed["camera.mcap"]
    fake_cross_device(monkeypatch, {lidar["path"], cam["path"]})
    dest = tmp_path / "other_disk"
    dest.mkdir()
    before = sorted(p.name for p in os.scandir(lidar["path"]))

    progress = []
    move.move_bag(conn, lidar["id"], dest / "run_lidar", progress.append)
    move.move_bag(conn, cam["id"], dest / "camera.mcap")
    assert sorted(p.name for p in os.scandir(dest / "run_lidar")) == before
    assert not os.path.exists(lidar["path"]) and not os.path.exists(cam["path"])
    assert progress[-1] == 1.0 and all(a <= b for a, b in zip(progress, progress[1:]))
    assert [p.name for p in dest.iterdir() if p.name.startswith(".")] == []
    assert catalog.scan(conn, [dest]).unchanged == 2  # size and mtime preserved


def test_cancelled_copy_leaves_source_untouched(conn, indexed, tmp_path, monkeypatch):
    lidar = indexed["run_lidar"]
    fake_cross_device(monkeypatch, {lidar["path"]})
    dest = tmp_path / "other_disk"
    dest.mkdir()

    def cancel(frac):
        if frac > 0:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        move.move_bag(conn, lidar["id"], dest / "run_lidar", cancel)
    assert os.path.isdir(lidar["path"]) and list(dest.iterdir()) == []
    assert catalog.get_bag(conn, lidar["id"])["path"] == lidar["path"]


def test_run_moves_continues_after_failure(conn, bag_root, indexed, tmp_path):
    items = move.plan_moves(conn, [indexed["run_lidar"], indexed["old.db3"]], tmp_path / "d")
    (bag_root / "loose" / "old.db3").unlink()  # vanishes after planning
    move.run_moves(conn, items)
    assert [i.status for i in items] == ["done", "failed"] and items[1].message == "files not found"


def test_cli_mv(tmp_path, bag_root):
    dbp = tmp_path / "c.db"
    runner = CliRunner()

    def run(*args):
        return runner.invoke(main, ["--db", str(dbp), *map(str, args)])

    run("scan", bag_root)
    lidar = bag_root / "2025-10-09" / "run_lidar"
    dest = tmp_path / "dest"

    r = run("mv", "-n", lidar, dest)
    assert r.exit_code == 0 and "would" in r.output and lidar.exists()
    r = run("mv", lidar, dest)  # dest does not exist: rename
    assert r.exit_code == 0, r.output
    assert dest.is_dir() and "outside the registered roots" in r.output  # tmp_path is not a registered root
    assert "moved 1" in r.output

    r = run("mv", bag_root / "loose" / "camera.mcap", bag_root / "loose" / "old.db3", tmp_path / "many")
    assert r.exit_code == 0, r.output
    assert sorted(p.name for p in (tmp_path / "many").iterdir()) == ["camera.mcap", "old.db3"]
    assert "outside the registered roots" in r.output

    r = run("mv", dest, tmp_path / "many")
    assert r.exit_code == 0
    r = run("mv", tmp_path / "many" / "dest", tmp_path / "many" / "camera.mcap")
    assert r.exit_code != 0 and "already exists" in r.output


def test_web_move(tmp_path, bag_root):
    client = TestClient(create_app(tmp_path / "c.db"))
    client.post("/scan/roots", data={"path": str(bag_root)})
    client.post("/scan/run", data={})
    wait_for_job(client)
    ids = {b["name"]: b["id"] for b in client.get("/api/bags").json()}
    assert 'formaction="/move"' in client.get("/").text
    assert 'action="/bags/%d/move"' % ids["run_lidar"] in client.get(f"/bags/{ids['run_lidar']}").text

    url = f"/bags/{ids['run_lidar']}/move"
    assert "absolute path" in client.post(url, data={"dest": "rel"}).text
    assert "already there" in client.post(url, data={"dest": str(bag_root / "2025-10-09")}).text
    assert client.post(url, data={"dest": str(bag_root / "archive" / "lidar_renamed")}).url.path == "/jobs"
    job = wait_for_job(client)
    assert job["state"] == "completed" and job["stats"]["moved"] == 1
    assert client.get(f"/api/bags/{ids['run_lidar']}").json()["name"] == "lidar_renamed"

    chosen = [ids["camera.mcap"], ids["old.db3"]]
    assert "Move 2 bags" in client.get("/move", params={"bag": chosen}).text
    client.post("/move", data={"bag": chosen, "dest": str(tmp_path / "into")})
    job = wait_for_job(client)
    assert job["stats"] == {"moved": 2, "skipped": 0, "failed": 0}
    assert sorted(p.name for p in (tmp_path / "into").iterdir()) == ["camera.mcap", "old.db3"]
    assert "into" in client.get("/jobs").text
    assert "nothing to move" in client.post("/move", data={"bag": chosen, "dest": str(tmp_path / "into")}).text
