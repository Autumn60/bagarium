import os

import pytest
import zstandard
from click.testing import CliRunner
from fastapi.testclient import TestClient

from bagarium import catalog, extract, importer, scanner
from bagarium.cli import main
from bagarium.web import create_app

from conftest import T0, make_bag_dir, make_db3, make_mcap
from test_cli_web import wait_for_job


def truncate(path, keep):
    with open(path, "r+b") as f:
        f.truncate(keep)


def test_check_files(tmp_path):
    good = make_bag_dir(tmp_path / "good", {"/a": ("std_msgs/msg/String", 2000)}, storage="sqlite3")
    assert scanner.check_files(good) is None
    db3 = good / "good_0.db3"
    truncate(db3, db3.stat().st_size // 2)
    assert "good_0.db3: truncated (" in scanner.check_files(good)

    mcap = make_mcap(tmp_path / "ok.mcap", {"/a": ("std_msgs/msg/String", 3)})
    assert scanner.check_files(mcap) is None
    cut = make_mcap(tmp_path / "cut.mcap", {"/a": ("std_msgs/msg/String", 3)}, finish=False)
    assert scanner.check_files(cut).startswith("incomplete")
    (tmp_path / "junk.db3").write_bytes(b"x" * 200)
    assert scanner.check_files(tmp_path / "junk.db3") == "not an sqlite3 file"

    missing = make_bag_dir(tmp_path / "missing", {"/a": ("std_msgs/msg/String", 2)})
    (missing / "missing_0.mcap").unlink()
    assert scanner.check_files(missing) == "missing_0.mcap: missing"


@pytest.mark.parametrize("checksum", [False, True])
def test_check_zstd(tmp_path, checksum):
    data = os.urandom(300_000) + b"\x00" * 300_000  # several blocks, raw and RLE/compressed
    path = tmp_path / "x_0.db3.zstd"
    path.write_bytes(zstandard.ZstdCompressor(write_checksum=checksum).compress(data))
    assert scanner._check_file(path) is None
    with zstandard.ZstdCompressor().stream_writer(open(tmp_path / "s_0.db3.zstd", "wb")) as w:
        w.write(data[:100_000])
        w.flush(zstandard.FLUSH_FRAME)
        w.write(data[100_000:])  # a second frame, written in a streaming fashion
    assert scanner._check_file(tmp_path / "s_0.db3.zstd") is None
    for keep in (path.stat().st_size - 1, 200_000, 6, 3):  # shrinking only: growing would pad with zeros
        truncate(path, keep)
        assert "truncated" in scanner._check_file(path), keep


def break_run_db3(bag_root):
    """Truncate the data file of a bag directory whose metadata.yaml stays readable (the usual case)."""
    bag = bag_root / "2025-10-09" / "run_db3"
    make_db3(bag / "run_db3_0.db3.tmp", {"/chatter": ("std_msgs/msg/String", 3000)})
    (bag / "run_db3_0.db3.tmp").replace(bag / "run_db3_0.db3")  # big enough to have many pages
    truncate(bag / "run_db3_0.db3", 4096)
    return bag


def test_scan_records_problems_once(conn, bag_root, monkeypatch):
    bag = break_run_db3(bag_root)
    catalog.scan(conn, [bag_root])
    row = catalog.get_bag(conn, bag)
    assert row["problem"].startswith("run_db3_0.db3: truncated") and row["error"] is None
    assert catalog.broken_reason(row).startswith("broken")
    assert [r["name"] for r in catalog.search(conn, broken=True)] == ["run_db3"]

    calls = []
    real = scanner.check_files
    monkeypatch.setattr(scanner, "check_files", lambda p: calls.append(p) or real(p))
    assert catalog.scan(conn, [bag_root]).unchanged == 4 and calls == []  # checked bags are not re-checked
    conn.execute("UPDATE bags SET checked_mtime = NULL")  # e.g. a catalog from before the check existed
    assert catalog.scan(conn, [bag_root]).updated == 4 and len(calls) == 4


def test_batches_skip_broken_bags(conn, bag_root, tmp_path):
    bag = break_run_db3(bag_root)
    catalog.scan(conn, [bag_root])
    rows = catalog.search(conn)
    items = extract.plan_batch(conn, rows, output_dir=tmp_path / "o")
    skipped = {i.bag["name"]: i.message for i in items if i.status == "skipped"}
    assert list(skipped) == ["run_db3"] and skipped["run_db3"].startswith("broken: run_db3_0.db3: truncated")
    # one bag given explicitly is still tried
    [single] = extract.plan_batch(conn, [catalog.get_bag(conn, bag)], output_dir=tmp_path / "o")
    assert single.status == "pending"


def test_import_skips_broken_sources(conn, tmp_path):
    ssd = tmp_path / "ssd"
    make_bag_dir(ssd / "ok", {"/a": ("std_msgs/msg/String", 2)})
    bad = make_bag_dir(ssd / "bad", {"/a": ("std_msgs/msg/String", 2000)}, storage="sqlite3")
    truncate(bad / "bad_0.db3", 8192)
    items = {i.source.name: i for i in importer.plan_import(conn, ssd, tmp_path / "dest")}
    assert items["ok"].status == "pending"
    assert items["bad"].status == "skipped" and items["bad"].message.startswith("broken: bad_0.db3: truncated")
    [only] = importer.plan_import(conn, bad, tmp_path / "dest")  # explicitly that bag: allowed
    assert only.status == "pending"


def test_broken_in_cli_and_web(tmp_path, bag_root):
    bag = break_run_db3(bag_root)
    truncate(bag_root / "loose" / "old.db3", 4096)  # also unreadable: still shown as broken
    dbp = tmp_path / "c.db"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(dbp), "scan", str(bag_root)])
    out = runner.invoke(main, ["--db", str(dbp), "ls", "--broken"]).output
    assert "run_db3 [broken]" in out and "old.db3 [broken]" in out and "run_lidar" not in out
    assert "problem  run_db3_0.db3: truncated" in runner.invoke(main, ["--db", str(dbp), "show", str(bag)]).output

    client = TestClient(create_app(dbp))
    page = client.get("/").text
    assert page.count(">broken</span>") == 2 and 'name="broken"' in page
    assert sorted(b["name"] for b in client.get("/api/bags", params={"broken": "true"}).json()) == ["old.db3", "run_db3"]
    bag_id = next(b["id"] for b in client.get("/api/bags").json() if b["name"] == "run_db3")
    assert "Batch operations skip this bag" in client.get(f"/bags/{bag_id}").text
