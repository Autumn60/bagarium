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


def wait_for_job(client, timeout=10.0):
    """Wait until the queue is idle and return the most recently submitted job."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        jobs = client.get("/api/jobs").json()
        if not any(j["state"] in ("queued", "running") for j in jobs):
            return max(jobs, key=lambda j: j["id"])
        assert time.monotonic() < deadline, "jobs did not finish"
        time.sleep(0.05)


def test_web_scan(tmp_path, bag_root):
    client = TestClient(create_app(tmp_path / "c.db"))
    assert "No folders registered" in client.get("/scan").text

    assert "registered" in client.post("/scan/run", data={}).text  # nothing registered yet
    assert "not a directory" in client.post("/scan/roots", data={"path": str(bag_root / "nope")}).text
    client.post("/scan/roots", data={"path": str(bag_root)})
    assert "not a registered root" in client.post("/scan/run", data={"root": "/etc"}).text

    client.post("/scan/run", data={"root": str(bag_root)})
    status = wait_for_job(client)
    assert status["state"] == "completed" and status["stats"]["added"] == 4 and status["total"] == 4
    page = client.get("/jobs").text
    assert "completed" in page and "added 4" in page
    assert len(client.get("/api/bags").json()) == 4

    client.post("/scan/run", data={"force": "true"})
    assert wait_for_job(client)["stats"]["updated"] == 4

    client.post("/scan/roots/remove", data={"path": str(bag_root)})
    assert "No folders registered" in client.get("/scan").text
    assert len(client.get("/api/bags").json()) == 4


def test_web_queue_and_cancel(tmp_path, bag_root, monkeypatch):
    import threading

    from bagarium import catalog

    gate = threading.Event()
    original = catalog._index_one

    def slow_index_one(*args):
        gate.wait(5)
        return original(*args)

    monkeypatch.setattr(catalog, "_index_one", slow_index_one)
    client = TestClient(create_app(tmp_path / "c.db"))
    client.post("/scan/roots", data={"path": str(bag_root)})
    assert client.post("/scan/run", data={}).url.path == "/jobs"
    client.post("/scan/run", data={"force": "true"})  # queued behind the first
    client.post("/scan/run", data={})
    first, second, third = sorted(client.get("/api/jobs").json(), key=lambda j: j["id"])
    assert (first["state"], second["state"], third["state"]) == ("running", "queued", "queued")
    page = client.get("/jobs").text
    assert "Queued" in page and "Remove" in page and 'class="count">3<' in page

    client.post(f"/jobs/{second['id']}/cancel")  # drop a queued job
    assert client.get(f"/api/jobs/{second['id']}").json()["state"] == "cancelled"
    client.post(f"/jobs/{first['id']}/cancel")  # stop the running one
    gate.set()
    wait_for_job(client)
    states = {j["id"]: j for j in client.get("/api/jobs").json()}
    assert states[first["id"]]["state"] == "cancelled" and states[first["id"]]["done"] == 1
    assert states[third["id"]]["state"] == "completed"  # the queue moved on

    client.post("/jobs/clear")
    assert client.get("/api/jobs").json() == []
    assert client.get("/api/jobs/999").status_code == 404


def test_web_extract(tmp_path, bag_root):
    client = TestClient(create_app(tmp_path / "c.db"))
    client.post("/scan/roots", data={"path": str(bag_root)})
    client.post("/scan/run", data={})
    wait_for_job(client)
    bag_id = next(b["id"] for b in client.get("/api/bags").json() if b["name"] == "run_lidar")
    page = client.get(f"/bags/{bag_id}").text
    assert 'id="extract-form"' in page and 'name="topic" value="/tf"' in page

    url = f"/bags/{bag_id}/extract"
    out = tmp_path / "web_out"
    assert "select at least one topic" in client.post(url, data={"output": str(out)}).text
    assert "invalid time" in client.post(url, data={"topic": ["/tf"], "start": "soon"}).text
    assert "start is after end" in client.post(url, data={"topic": ["/tf"], "start": "2", "end": "1"}).text
    assert "already exists" in client.post(url, data={"topic": ["/tf"], "output": str(tmp_path)}).text

    client.post(url, data={"topic": ["/tf"], "start": "0.5", "end": "1:00", "output": str(out)})
    status = wait_for_job(client)
    assert status["state"] == "completed", status
    assert status["stats"]["messages"] == 16  # /tf at 0.1 s steps from 0.5 s to 2.0 s
    new = client.get(f"/api/bags/{status['results'][0]['result_bag_id']}").json()
    assert new["path"] == str(out) and [t["name"] for t in new["topics"]] == ["/tf"]
    assert "Extracted from" in new["note"]
    assert f'href="/bags/{new["id"]}"' in client.get(f"/bags/{bag_id}").text


def test_web_batch_extract(tmp_path, bag_root):
    client = TestClient(create_app(tmp_path / "c.db"))
    client.post("/scan/roots", data={"path": str(bag_root)})
    client.post("/scan/run", data={})
    wait_for_job(client)
    ids = {b["name"]: b["id"] for b in client.get("/api/bags").json()}
    assert 'name="bag" value=' in client.get("/").text

    chosen = [ids["run_lidar"], ids["camera.mcap"], ids["old.db3"]]
    page = client.get("/extract", params={"bag": chosen}).text
    assert "Extract from 3 bags" in page and 'value="/lidar/points"' in page and "1 / 3" in page

    def post(**data):
        return client.post("/extract", data={"bag": chosen, **data})

    assert "select at least one topic" in post().text
    assert "absolute path" in post(topic=["/tf"], output_mode="dir", output_dir="rel").text
    assert "nothing to extract" in post(topic=["/nope"]).text

    out_dir = tmp_path / "outs"
    r = post(topic=["/tf", "/odom"], start="0", end="0.5", output_mode="dir", output_dir=str(out_dir), suffix="w")
    assert r.url.path == "/jobs"
    status = wait_for_job(client)
    assert status["state"] == "completed", status
    assert status["stats"] == {"done": 2, "skipped": 1, "failed": 0, "messages": 12}
    assert sorted(p.name for p in out_dir.iterdir()) == ["old_w", "run_lidar_w"]
    results = {r["source_id"]: r for r in status["results"]}
    assert results[ids["camera.mcap"]]["status"] == "skipped"
    jobs_page = client.get("/jobs").text
    assert "run_lidar_w" in jobs_page and "no topics match" in jobs_page


def test_queued_extractions_do_not_collide(tmp_path, bag_root, monkeypatch):
    import threading

    from bagarium import extract

    gate = threading.Event()
    original = extract.extract_bag

    def slow_extract_bag(*args, **kwargs):
        gate.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(extract, "extract_bag", slow_extract_bag)
    client = TestClient(create_app(tmp_path / "c.db"))
    client.post("/scan/roots", data={"path": str(bag_root)})
    client.post("/scan/run", data={})
    wait_for_job(client)
    bag_id = next(b["id"] for b in client.get("/api/bags").json() if b["name"] == "run_lidar")
    out = tmp_path / "outs"
    form = {"bag": [bag_id], "topic": ["/tf"], "output_mode": "dir", "output_dir": str(out), "suffix": "same"}
    client.post("/extract", data=form)
    client.post("/extract", data=form)  # same names, queued while the first has not written anything
    exact = {"topic": ["/tf"], "output": str(out / "run_lidar_same")}
    assert "already used by a queued job" in client.post(f"/bags/{bag_id}/extract", data=exact).text
    gate.set()
    wait_for_job(client)
    assert sorted(p.name for p in out.iterdir()) == ["run_lidar_same", "run_lidar_same_2"]
