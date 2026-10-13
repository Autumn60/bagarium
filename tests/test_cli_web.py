import json

from click.testing import CliRunner
from fastapi.testclient import TestClient

from bagarium.cli import main
from bagarium.web import create_app


def run(db_path, *args):
    result = CliRunner().invoke(main, ["--db", str(db_path), *map(str, args)])
    assert result.exit_code == 0, result.output
    return result.output


def test_cli_flow(tmp_path, bag_root):
    dbp = tmp_path / "c.db"
    assert "added 4" in run(dbp, "scan", bag_root)
    out = json.loads(run(dbp, "ls", "--json"))
    bag_id = next(b["id"] for b in out if b["name"] == "run_lidar")

    run(dbp, "tag", bag_id, "outdoor", "lidar")
    run(dbp, "note", bag_id, "hello world")
    assert run(dbp, "ls", "-t", "outdoor", "--paths").strip().endswith("run_lidar")
    shown = run(dbp, "show", bag_id)
    assert "hello world" in shown and "/lidar/points" in shown and "lidar, outdoor" in shown
    run(dbp, "untag", bag_id, "lidar")
    assert "outdoor" in run(dbp, "tags") and "lidar" not in run(dbp, "tags")

    result = CliRunner().invoke(main, ["--db", str(dbp), "show", "9999"])
    assert result.exit_code != 0 and "not found" in result.output


def test_cli_roots(tmp_path, bag_root):
    dbp = tmp_path / "c.db"
    result = CliRunner().invoke(main, ["--db", str(dbp), "scan"])
    assert result.exit_code != 0 and "no roots registered" in result.output

    run(dbp, "scan", "--no-register", bag_root / "loose")
    assert "no roots" in CliRunner().invoke(main, ["--db", str(dbp), "roots"]).output

    run(dbp, "scan", bag_root)
    assert str(bag_root) in run(dbp, "roots")
    assert "unchanged 4" in run(dbp, "scan")  # no args: registered roots
    run(dbp, "roots", "rm", bag_root)
    run(dbp, "roots", "add", bag_root / "loose")
    assert "loose" in run(dbp, "roots")


def test_web(tmp_path, bag_root):
    dbp = tmp_path / "c.db"
    run(dbp, "scan", bag_root)
    client = TestClient(create_app(dbp))

    page = client.get("/")
    assert page.status_code == 200 and "run_lidar" in page.text

    bag_id = next(b["id"] for b in client.get("/api/bags").json() if b["name"] == "run_lidar")
    r = client.post(f"/bags/{bag_id}/tags", data={"tags": "outdoor, night"}, follow_redirects=False)
    assert r.status_code == 303
    client.post(f"/bags/{bag_id}/note", data={"note": "<b>x</b>"})
    detail = client.get(f"/bags/{bag_id}")
    assert "&lt;b&gt;x&lt;/b&gt;" in detail.text and "/lidar/points" in detail.text

    assert [b["name"] for b in client.get("/api/bags", params={"tag": ["outdoor", "night"]}).json()] == ["run_lidar"]
    client.post(f"/bags/{bag_id}/tags/remove", data={"tag": "night"})
    assert client.get(f"/api/bags/{bag_id}").json()["tags"] == ["outdoor"]

    assert client.get("/", params={"tag": "outdoor"}).text.count("/bags/") == 1
    assert "invalid date" in client.get("/", params={"since": "nope"}).text
    assert client.get("/api/bags", params={"since": "nope"}).status_code == 400
    assert client.get("/bags/9999").status_code == 404
    assert client.get("/tags").status_code == 200

    evil = client.post(f"/bags/{bag_id}/note", data={"note": "pwned"}, headers={"origin": "http://evil.example"})
    assert evil.status_code == 403


def wait_for_scan(client, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while (status := client.get("/api/scan").json())["running"]:
        assert time.monotonic() < deadline, "scan did not finish"
        time.sleep(0.05)
    return status


def test_web_scan(tmp_path, bag_root):
    client = TestClient(create_app(tmp_path / "c.db"))
    assert "No folders registered" in client.get("/scan").text

    assert "registered" in client.post("/scan/run", data={}).text  # nothing registered yet
    assert "not a directory" in client.post("/scan/roots", data={"path": str(bag_root / "nope")}).text
    client.post("/scan/roots", data={"path": str(bag_root)})
    assert "not a registered root" in client.post("/scan/run", data={"root": "/etc"}).text

    client.post("/scan/run", data={"root": str(bag_root)})
    status = wait_for_scan(client)
    assert status["outcome"] == "completed" and status["added"] == 4 and status["total"] == 4
    page = client.get("/scan").text
    assert "Last scan completed" in page and "added 4" in page
    assert len(client.get("/api/bags").json()) == 4

    client.post("/scan/run", data={"force": "true"})
    assert wait_for_scan(client)["updated"] == 4

    client.post("/scan/roots/remove", data={"path": str(bag_root)})
    assert "No folders registered" in client.get("/scan").text
    assert len(client.get("/api/bags").json()) == 4


def test_web_scan_cancel(tmp_path, bag_root):
    import threading

    app = create_app(tmp_path / "c.db")
    job = app.state.scan_job
    gate = threading.Event()
    original = job._on_bag

    def slow_on_bag(*args):
        gate.wait(5)
        original(*args)

    job._on_bag = slow_on_bag
    client = TestClient(app)
    client.post("/scan/roots", data={"path": str(bag_root)})
    client.post("/scan/run", data={})
    assert client.get("/api/scan").json()["running"]
    assert "already running" in client.post("/scan/run", data={}).text
    assert "Scanning" in client.get("/scan").text

    client.post("/scan/cancel")
    gate.set()
    status = wait_for_scan(client)
    assert status["outcome"] == "cancelled" and status["done"] == 1
