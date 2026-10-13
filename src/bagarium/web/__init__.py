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

from .. import __version__, catalog, db, fmt
from .scanjob import ScanJob

HERE = Path(__file__).parent


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="bagarium", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(
        time=fmt.time, size=fmt.size, count=fmt.count,
    )
    templates.env.globals.update(duration=fmt.duration, version=__version__)

    job = app.state.scan_job = ScanJob(db_path)

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
    def bag_detail(request: Request, bag_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        bag = load_bag(conn, bag_id)
        return templates.TemplateResponse(request, "bag.html", {
            "bag": bag,
            "tags": catalog.tags_of(conn, [bag_id])[bag_id],
            "topics": catalog.topics_of(conn, bag_id),
            "all_tags": catalog.all_tags(conn),
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

    # ------------------------------------------------------------ scanning

    def back_to_scan(error: str = "") -> RedirectResponse:
        return RedirectResponse("/scan" + ("?" + urlencode({"error": error}) if error else ""), status_code=303)

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
            return back_to_scan(str(e))
        return back_to_scan()

    @app.post("/scan/roots/remove", dependencies=[Depends(same_origin)])
    def remove_root(path: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)):
        catalog.remove_roots(conn, [path])
        return back_to_scan()

    @app.post("/scan/run", dependencies=[Depends(same_origin)])
    def run_scan(root: str = Form(""), force: bool = Form(False), conn: sqlite3.Connection = Depends(get_conn)):
        registered = [r["path"] for r in catalog.list_roots(conn)]
        if root and root not in registered:
            return back_to_scan(f"not a registered root: {root}")
        roots = [root] if root else registered
        if not roots:
            return back_to_scan("no folders registered; add one first")
        if not job.start(roots, force):
            return back_to_scan("a scan is already running")
        return back_to_scan()

    @app.post("/scan/cancel", dependencies=[Depends(same_origin)])
    def cancel_scan():
        job.cancel()
        return back_to_scan()

    # ------------------------------------------------------------ JSON

    @app.get("/api/scan")
    def api_scan_status():
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
