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
