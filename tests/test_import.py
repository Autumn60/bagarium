import os

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from bagarium import catalog, fileops, importer
from bagarium.cli import main
from bagarium.web import create_app

from conftest import T0, make_bag_dir, make_mcap
from test_cli_web import wait_for_job


@pytest.fixture
def ssd(tmp_path):
    """A removable disk with two same-named bags in different folders and a nested bag."""
    root = tmp_path / "ssd"
    make_bag_dir(root / "day1" / "run", {"/a": ("std_msgs/msg/String", 5)})
    make_bag_dir(root / "day2" / "run", {"/a": ("std_msgs/msg/String", 7)}, start=T0 + 86400 * 10**9)
    make_bag_dir(root / "day2" / "run" / "derivatives" / "cut", {"/a": ("std_msgs/msg/String", 2)})
    make_mcap(root / "loose.mcap", {"/b": ("std_msgs/msg/Int32", 3)})
    return root


def snapshot(path):
    return sorted((str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime) for p in path.rglob("*"))


def test_plan_import_keeps_layout(conn, ssd, tmp_path):
    dest = tmp_path / "local"
    items = importer.plan_import(conn, ssd, dest)
    assert sorted(str(i.target.relative_to(dest)) for i in items) == [
        "day1/run", "day2/run", "day2/run/derivatives/cut", "loose.mcap"]
    assert all(i.status == "pending" for i in items)
    # the nested bag is not counted in its parent's size
    parent = next(i for i in items if i.target == dest / "day2" / "run")
    assert parent.size == sum(p.stat().st_size for p in (ssd / "day2" / "run").iterdir() if p.is_file())

    [single] = importer.plan_import(conn, ssd / "day1" / "run", dest)
    assert single.target == dest / "run"

    with pytest.raises(ValueError, match="inside the source"):
        importer.plan_import(conn, ssd, ssd / "copy")
    with pytest.raises(ValueError, match="already inside"):
        importer.plan_import(conn, ssd / "day1", ssd)
    with pytest.raises(ValueError, match="does not exist"):
        importer.plan_import(conn, tmp_path / "nope", dest)


def test_import_copies_and_is_idempotent(conn, ssd, tmp_path):
    before = snapshot(ssd)
    # the SSD was catalogued before: its tags and notes come along
    catalog.scan(conn, [ssd])
    src_id = catalog.get_bag(conn, ssd / "day1" / "run")["id"]
    catalog.add_tags(conn, src_id, ["field"])
    catalog.set_note(conn, src_id, "windy")

    dest = tmp_path / "local"
    items = importer.plan_import(conn, ssd, dest)
    copied = []
    importer.run_import(conn, items, on_progress=lambda k, n, c, i: copied.append(c))
    assert [i.status for i in items] == ["done"] * 4
    assert copied[-1] == importer.pending_bytes([]) + sum(i.size for i in items)
    assert snapshot(ssd) == before  # source untouched
    assert snapshot(dest / "day2" / "run" / "derivatives") == snapshot(ssd / "day2" / "run" / "derivatives")
    assert [p.name for p in dest.rglob(".bagarium-*")] == []

    new = catalog.get_bag(conn, dest / "day1" / "run")
    assert new["note"] == "windy" and catalog.tags_of(conn, [new["id"]])[new["id"]] == ["field"]
    assert catalog.scan(conn, [dest]).unchanged == 4  # timestamps kept

    again = importer.plan_import(conn, ssd, dest)
    assert {i.status for i in again} == {"skipped"}
    assert all(i.message.startswith("already imported") for i in again)

    # a new recording on the SSD is the only thing imported next time
    make_mcap(ssd / "day3.mcap", {"/c": ("std_msgs/msg/Empty", 4)}, start=T0 + 3 * 86400 * 10**9)
    assert [i.source.name for i in importer.plan_import(conn, ssd, dest) if i.status == "pending"] == ["day3.mcap"]


def test_failed_parent_skips_nested_and_cancel_cleans_up(conn, ssd, tmp_path, monkeypatch):
    dest = tmp_path / "local"
    items = importer.plan_import(conn, ssd / "day2", dest)
    assert [i.source.name for i in items] == ["run", "cut"]  # parents come before nested bags
    real = fileops.copy_staged

    def failing(src, target, files, on_bytes=None):
        if src.name == "run":
            raise OSError("disk error")
        return real(src, target, files, on_bytes)

    monkeypatch.setattr(fileops, "copy_staged", failing)
    importer.run_import(conn, items)
    assert [(i.status, i.message) for i in items] == [
        ("failed", "OSError: disk error"), ("skipped", "the enclosing bag failed to import")]
    monkeypatch.setattr(fileops, "copy_staged", real)

    items = importer.plan_import(conn, ssd, tmp_path / "other")

    def cancel(k, n, copied, item):
        if copied:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        importer.run_import(conn, items, on_progress=cancel)
    assert not (tmp_path / "other").exists() or not any((tmp_path / "other").rglob("*.yaml"))


def test_cli_import(tmp_path, ssd, monkeypatch):
    dbp = tmp_path / "c.db"
    dest = tmp_path / "local"
    runner = CliRunner()

    def run(*args):
        return runner.invoke(main, ["--db", str(dbp), *map(str, args)])

    r = run("import", "-n", ssd, dest)
    assert r.exit_code == 0 and r.output.count("copy  ") == 4 and not dest.exists()

    monkeypatch.setattr(fileops, "free_bytes", lambda p: 10)
    r = run("import", ssd, dest)
    assert r.exit_code != 0 and "not enough free space" in r.output
    monkeypatch.undo()

    r = run("import", ssd, dest)
    assert r.exit_code == 0, r.output
    assert "imported 4, skipped 0, failed 0" in r.output
    assert str(dest) in run("roots").output
    r = run("import", ssd, dest)
    assert r.exit_code == 0 and "0 bag(s) to copy" in r.output


def test_web_import(tmp_path, ssd):
    client = TestClient(create_app(tmp_path / "c.db"))
    dest = tmp_path / "local"
    assert "Import" in client.get("/import").text

    page = client.get("/import", params={"src": str(ssd), "dest": str(dest)}).text
    assert "4 bags found · 4 can be copied" in page and "Queue import" in page
    assert page.count('name="select"') == 4 and "day2/run/derivatives/cut" in page
    assert "inside the source" in client.get("/import", params={"src": str(ssd), "dest": str(ssd / "x")}).text

    form = {"src": str(ssd), "dest": str(dest)}
    assert "select at least one bag" in client.post("/import", data=form).text
    chosen = [str(ssd / "day2" / "run" / "derivatives" / "cut"), str(ssd / "loose.mcap")]
    r = client.post("/import", data={**form, "select": chosen})
    assert r.url.path == "/jobs"
    job = wait_for_job(client)
    assert job["state"] == "completed" and job["stats"] == {"imported": 2, "skipped": 0, "failed": 0}
    assert sorted(str(p.relative_to(dest)) for p in dest.rglob("metadata.yaml")) == ["day2/run/derivatives/cut/metadata.yaml"]

    # the rest can be picked later, including the parent of the nested bag already imported
    page = client.get("/import", params=form).text
    assert "4 bags found · 2 can be copied" in page and page.count('name="select"') == 2
    client.post("/import", data={**form, "select": [str(ssd / "day1" / "run"), str(ssd / "day2" / "run")]})
    job = wait_for_job(client)
    assert job["stats"]["imported"] == 2, job
    assert len(client.get("/api/bags", params={"root": str(dest)}).json()) == 4
    assert "nothing to import" in client.post("/import", data={**form, "select": chosen}).text


def test_import_patterns_and_nested_first(conn, ssd, tmp_path):
    dest = tmp_path / "local"
    items = importer.plan_import(conn, ssd, dest, include=["day2/*"], exclude=["*/derivatives/*"])
    assert [str(i.target.relative_to(dest)) for i in items] == ["day2/run"]
    assert [i.source.name for i in importer.plan_import(conn, ssd, dest, include=["*.mcap"])] == ["loose.mcap"]

    nested = importer.plan_import(conn, ssd, dest, include=["*/derivatives/*"])
    importer.run_import(conn, nested)
    [parent] = importer.plan_import(conn, ssd, dest, include=["day2/run"])
    assert parent.status == "pending"  # dest/day2/run only holds the nested bag's folder
    importer.run_import(conn, [parent])
    assert parent.status == "done", parent.message
    assert sorted(p.name for p in (dest / "day2" / "run").iterdir()) == sorted(
        p.name for p in (ssd / "day2" / "run").iterdir())
    assert catalog.get_bag(conn, dest / "day2" / "run")["message_count"] == 7


def test_cli_import_patterns(tmp_path, ssd):
    dbp = tmp_path / "c.db"
    r = CliRunner().invoke(main, ["--db", str(dbp), "import", str(ssd), str(tmp_path / "local"), "-i", "day1/*"])
    assert r.exit_code == 0 and "imported 1," in r.output
    r = CliRunner().invoke(main, ["--db", str(dbp), "import", str(ssd), str(tmp_path / "local"), "-i", "nope/*"])
    assert r.exit_code != 0 and "matching the patterns" in r.output
