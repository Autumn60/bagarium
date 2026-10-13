"""Local web UI (FastAPI + server-rendered Jinja2 templates)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, catalog, db, extract, fmt
from .jobs import MAX_ERRORS, JobRunner

HERE = Path(__file__).parent


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="bagarium", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(
        time=fmt.time, size=fmt.size, count=fmt.count,
    )
    templates.env.globals.update(duration=fmt.duration, version=__version__)

    job = app.state.jobs = JobRunner()

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def same_origin(request: Request) -> None:
        # The UI only listens locally, but a page on another site could still
        # submit forms to it; reject cross-origin writes.
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "cross-origin request rejected")

    def load_bag(conn: sqlite3.Connection, bag_id: int) -> sqlite3.Row:
        row = catalog.get_bag(conn, bag_id)
        if row is None:
            raise HTTPException(404, "bag not found")
        return row

    def run_search(conn, q, tag, topic, type_, since, until, missing):
        errors = []

        def parse(value: str, end: bool):
            if not value:
                return None
            try:
                return catalog.parse_time(value, end_of_day=end)
            except ValueError:
                errors.append(f"invalid date: {value}")
                return None

        rows = catalog.search(
            conn, q, tag, [t for t in [topic] if t], [t for t in [type_] if t],
            parse(since, False), parse(until, True), missing,
        )
        return rows, errors

    # ------------------------------------------------------------ HTML

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        q: str = "",
        tag: list[str] = Query(default=[]),
        topic: str = "",
        type: str = "",
        since: str = "",
        until: str = "",
        missing: bool = False,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        tag = [t for t in tag if t]
        rows, errors = run_search(conn, q, tag, topic, type, since, until, missing)
        params = {"q": q, "tag": tag, "topic": topic, "type": type, "since": since, "until": until, "missing": missing}

        def with_tags(new_tags: list[str]) -> str:
            p = {k: v for k, v in params.items() if v and k != "tag"}
            return "/?" + urlencode({**p, "tag": new_tags}, doseq=True)

        return templates.TemplateResponse(request, "index.html", {
            "bags": rows,
            "tag_map": catalog.tags_of(conn, (r["id"] for r in rows)),
            "all_tags": catalog.all_tags(conn),
            "params": params,
            "errors": errors,
            "with_tags": with_tags,
            "total_size": sum(r["size_bytes"] for r in rows),
        })

    @app.get("/bags/{bag_id}", response_class=HTMLResponse)
    def bag_detail(request: Request, bag_id: int, error: str = "", conn: sqlite3.Connection = Depends(get_conn)):
        bag = load_bag(conn, bag_id)
        return templates.TemplateResponse(request, "bag.html", {
            "bag": bag,
            "tags": catalog.tags_of(conn, [bag_id])[bag_id],
            "topics": catalog.topics_of(conn, bag_id),
            "all_tags": catalog.all_tags(conn),
            "job": job.status(),
            "error": error,
            "default_output": extract.default_output_path(Path(bag["path"])),
        })

    @app.post("/bags/{bag_id}/note", dependencies=[Depends(same_origin)])
    def update_note(bag_id: int, note: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
        catalog.set_note(conn, load_bag(conn, bag_id)["id"], note)
        return RedirectResponse(f"/bags/{bag_id}", status_code=303)

    @app.post("/bags/{bag_id}/tags", dependencies=[Depends(same_origin)])
    def add_tags(bag_id: int, tags: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
        catalog.add_tags(conn, load_bag(conn, bag_id)["id"], tags.split(","))
        return RedirectResponse(f"/bags/{bag_id}", status_code=303)

    @app.post("/bags/{bag_id}/tags/remove", dependencies=[Depends(same_origin)])
    def remove_tag(bag_id: int, tag: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)):
        catalog.remove_tags(conn, load_bag(conn, bag_id)["id"], [tag])
        return RedirectResponse(f"/bags/{bag_id}", status_code=303)

    @app.get("/tags", response_class=HTMLResponse)
    def tags_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return templates.TemplateResponse(request, "tags.html", {"all_tags": catalog.all_tags(conn)})

    # ------------------------------------------------------------ jobs

    def redirect(url: str, error: str = "") -> RedirectResponse:
        return RedirectResponse(url + ("?" + urlencode({"error": error}) if error else ""), status_code=303)

    @app.post("/jobs/cancel", dependencies=[Depends(same_origin)])
    def cancel_job(next: str = Form("/scan")):
        job.cancel()
        return redirect(next if next.startswith("/") and not next.startswith("//") else "/scan")

    # ------------------------------------------------------------ scanning

    def scan_target(roots: list[str], force: bool):
        def run(j: JobRunner) -> dict:
            j.set_stats(added=0, updated=0, moved=0, unchanged=0, errors=0)

            def on_bag(path: Path, status: str, done: int, total: int) -> None:
                j.update(done=done, total=total, current=str(path))
                j.bump("errors" if status == "error" else status)
                j.check_cancelled()

            conn = db.connect(db_path)
            try:
                result = catalog.scan(conn, roots, force=force, on_bag=on_bag)
            finally:
                conn.close()
            j.set_stats(missing=result.missing, errors=len(result.errors))
            return {"errors": [{"path": str(p), "message": m} for p, m in result.errors[:MAX_ERRORS]]}

        return run

    @app.get("/scan", response_class=HTMLResponse)
    def scan_page(request: Request, error: str = "", conn: sqlite3.Connection = Depends(get_conn)):
        return templates.TemplateResponse(request, "scan.html", {
            "roots": catalog.list_roots(conn), "job": job.status(), "error": error,
        })

    @app.post("/scan/roots", dependencies=[Depends(same_origin)])
    def add_root(path: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)):
        try:
            catalog.add_roots(conn, [path.strip()])
        except ValueError as e:
            return redirect("/scan", str(e))
        return redirect("/scan")

    @app.post("/scan/roots/remove", dependencies=[Depends(same_origin)])
    def remove_root(path: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)):
        catalog.remove_roots(conn, [path])
        return redirect("/scan")

    @app.post("/scan/run", dependencies=[Depends(same_origin)])
    def run_scan(root: str = Form(""), force: bool = Form(False), conn: sqlite3.Connection = Depends(get_conn)):
        registered = [r["path"] for r in catalog.list_roots(conn)]
        if root and root not in registered:
            return redirect("/scan", f"not a registered root: {root}")
        roots = [root] if root else registered
        if not roots:
            return redirect("/scan", "no folders registered; add one first")
        label = f"Scan {roots[0]}" if len(roots) == 1 else f"Scan {len(roots)} folders"
        if force:
            label = "Re" + label[0].lower() + label[1:]
        if not job.start("scan", label, scan_target(roots, force)):
            return redirect("/scan", "another job is already running")
        return redirect("/scan")

    # ------------------------------------------------------------ extraction

    @app.post("/bags/{bag_id}/extract", dependencies=[Depends(same_origin)])
    def run_extract(
        bag_id: int,
        start: str = Form(""),
        end: str = Form(""),
        topic: list[str] = Form(default=[]),
        output: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        bag = load_bag(conn, bag_id)
        here = f"/bags/{bag_id}"
        try:
            start_ns = extract.parse_bag_time(start, bag["start_ns"]) if start.strip() else None
            end_ns = extract.parse_bag_time(end, bag["start_ns"]) if end.strip() else None
        except ValueError as e:
            return redirect(here, f"invalid time: {e}")
        if start_ns is not None and end_ns is not None and start_ns > end_ns:
            return redirect(here, "start is after end")
        if not topic:
            return redirect(here, "select at least one topic")
        out = Path(output.strip()).expanduser() if output.strip() else extract.default_output_path(Path(bag["path"]))
        if not out.is_absolute():
            return redirect(here, "output path must be absolute")
        if out.exists():
            return redirect(here, f"output already exists: {out}")
        all_topics = {t["name"] for t in catalog.topics_of(conn, bag_id)}
        include = [] if set(topic) >= all_topics else topic

        def run(j: JobRunner) -> dict:
            j.update(total=1000, current=str(out))

            def on_progress(frac: float, written: int) -> None:
                j.update(done=round(frac * 1000))
                j.set_stats(messages=written)
                j.check_cancelled()

            c = db.connect(db_path)
            try:
                result, new_id = extract.extract_bag(c, dict(bag), out, start_ns, end_ns, include, (), on_progress)
            finally:
                c.close()
            return {"result_bag_id": new_id, "result_path": str(result.path)}

        if not job.start("extract", f"Extract from {bag['name']}", run, bag_id=bag_id):
            return redirect(here, "another job is already running")
        return redirect(here)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return templates.TemplateResponse(request, "jobs.html", {"job": job.status()})

    def load_bags(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
        rows = [catalog.get_bag(conn, i) for i in dict.fromkeys(ids)]
        return [r for r in rows if r is not None]

    @app.get("/extract", response_class=HTMLResponse)
    def batch_extract_page(
        request: Request,
        bag: list[int] = Query(default=[]),
        error: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        bags = load_bags(conn, bag)
        topics = []
        if bags:
            ids = [b["id"] for b in bags]
            topics = conn.execute(
                f"SELECT name, GROUP_CONCAT(DISTINCT type) AS types, COUNT(DISTINCT bag_id) AS bag_count,"
                f" SUM(message_count) AS message_count FROM topics WHERE bag_id IN ({','.join('?' * len(ids))})"
                f" GROUP BY name ORDER BY name",
                ids,
            ).fetchall()
        return templates.TemplateResponse(request, "extract.html", {
            "bags": bags, "topics": topics, "job": job.status(), "error": error,
            "suffix": extract.default_suffix(),
        })

    @app.post("/extract", dependencies=[Depends(same_origin)])
    def run_batch_extract(
        bag: list[int] = Form(default=[]),
        topic: list[str] = Form(default=[]),
        start: str = Form(""),
        end: str = Form(""),
        output_mode: str = Form("source"),
        output_dir: str = Form(""),
        suffix: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        back = "/extract?" + urlencode({"bag": bag}, doseq=True)

        def fail(message: str) -> RedirectResponse:
            return RedirectResponse(back + "&" + urlencode({"error": message}), status_code=303)

        bags = load_bags(conn, bag)
        if not bags:
            return redirect("/", "no bags selected")
        if not topic:
            return fail("select at least one topic")
        out_dir = None
        if output_mode == "dir":
            if not output_dir.strip() or not Path(output_dir.strip()).expanduser().is_absolute():
                return fail("output folder must be an absolute path")
            out_dir = Path(output_dir.strip()).expanduser()
        suffix = suffix.strip()
        if suffix and ("/" in suffix or suffix.startswith(".")):
            return fail("suffix must not contain '/' or start with '.'")

        ids = [b["id"] for b in bags]
        all_topics = {r[0] for r in conn.execute(
            f"SELECT DISTINCT name FROM topics WHERE bag_id IN ({','.join('?' * len(ids))})", ids)}
        include = [] if set(topic) >= all_topics else topic
        items = extract.plan_batch(conn, bags, start.strip() or None, end.strip() or None,
                                   include=include, output_dir=out_dir, suffix=suffix or None)
        if not any(i.status == "pending" for i in items):
            return fail("nothing to extract: " + "; ".join(sorted({i.message for i in items})))

        def run(j: JobRunner) -> dict:
            n = sum(i.status == "pending" for i in items)
            j.update(total=1000 * n)
            j.set_stats(done=0, skipped=sum(i.status == "skipped" for i in items), failed=0, messages=0)

            def on_progress(k, n, item, frac, written):
                j.update(done=round((k + frac) * 1000), current=f"[{k + 1}/{n}] {item.bag['path']}")
                j.check_cancelled()

            def on_item(item):
                j.bump(item.status)
                j.bump("messages", item.message_count)

            c = db.connect(db_path)
            try:
                extract.run_batch(c, items, include, (), on_progress, on_item)
            finally:
                c.close()
                j.update(results=[{
                    "source": i.bag["path"], "source_id": i.bag["id"], "status": i.status,
                    "message": i.message, "output": str(i.output), "result_bag_id": i.result_id,
                } for i in items])
            return {}

        n = sum(i.status == "pending" for i in items)
        if not job.start("extract", f"Extract from {n} bag(s)", run,
                         results=[{"source": i.bag["path"], "source_id": i.bag["id"], "status": i.status,
                                   "message": i.message, "output": str(i.output), "result_bag_id": None}
                                  for i in items]):
            return fail("another job is already running")
        return redirect("/jobs")

    # ------------------------------------------------------------ JSON

    @app.get("/api/job")
    def api_job_status():
        return job.status()

    @app.get("/api/bags")
    def api_bags(
        q: str = "",
        tag: list[str] = Query(default=[]),
        topic: str = "",
        type: str = "",
        since: str = "",
        until: str = "",
        missing: bool = False,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        rows, errors = run_search(conn, q, tag, topic, type, since, until, missing)
        if errors:
            raise HTTPException(400, "; ".join(errors))
        tag_map = catalog.tags_of(conn, (r["id"] for r in rows))
        return [{**dict(r), "missing": bool(r["missing"]), "tags": tag_map[r["id"]]} for r in rows]

    @app.get("/api/bags/{bag_id}")
    def api_bag(bag_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        r = load_bag(conn, bag_id)
        return {
            **dict(r),
            "missing": bool(r["missing"]),
            "tags": catalog.tags_of(conn, [bag_id])[bag_id],
            "topics": [dict(t) for t in catalog.topics_of(conn, bag_id)],
        }

    return app
