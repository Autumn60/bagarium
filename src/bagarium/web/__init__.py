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

from .. import __version__, catalog, db, extract, fileops, fmt, importer, move, track
from .jobs import MAX_ERRORS, Job, JobQueue

HERE = Path(__file__).parent


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="bagarium", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(
        time=fmt.time, size=fmt.size, count=fmt.count,
    )
    templates.env.globals.update(duration=fmt.duration, version=__version__)

    jobs = app.state.jobs = JobQueue()
    templates.env.globals.update(active_jobs=jobs.active_count)

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

    def run_search(conn, q, tag, topic, type_, since, until, missing, root=""):
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
            parse(since, False), parse(until, True), missing, root=root or None,
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
        root: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        tag = [t for t in tag if t]
        rows, errors = run_search(conn, q, tag, topic, type, since, until, missing, root)
        params = {"q": q, "tag": tag, "topic": topic, "type": type, "since": since, "until": until,
                  "missing": missing, "root": root}

        def with_tags(new_tags: list[str]) -> str:
            p = {k: v for k, v in params.items() if v and k != "tag"}
            return "/?" + urlencode({**p, "tag": new_tags}, doseq=True)

        return templates.TemplateResponse(request, "index.html", {
            "bags": rows,
            "tag_map": catalog.tags_of(conn, (r["id"] for r in rows)),
            "all_tags": catalog.all_tags(conn),
            "params": params,
            "roots": catalog.list_roots(conn),
            "errors": errors,
            "with_tags": with_tags,
            "total_size": sum(r["size_bytes"] for r in rows),
        })

    @app.get("/map", response_class=HTMLResponse)
    def map_page(
        request: Request,
        q: str = "",
        tag: list[str] = Query(default=[]),
        topic: str = "",
        type: str = "",
        since: str = "",
        until: str = "",
        root: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        tag = [t for t in tag if t]
        rows, errors = run_search(conn, q, tag, topic, type, since, until, False, root)
        with_track = [r for r in rows if track.candidates(catalog.topics_of(conn, r["id"]))]
        params = {"q": q, "tag": tag, "topic": topic, "type": type, "since": since, "until": until, "root": root}
        return templates.TemplateResponse(request, "map.html", {
            "bags": with_track, "total": len(rows), "errors": errors,
            "list_url": "/?" + urlencode({k: v for k, v in params.items() if v}, doseq=True),
        })

    @app.get("/bags/{bag_id}", response_class=HTMLResponse)
    def bag_detail(request: Request, bag_id: int, error: str = "", conn: sqlite3.Connection = Depends(get_conn)):
        bag = load_bag(conn, bag_id)
        return templates.TemplateResponse(request, "bag.html", {
            "bag": bag,
            "tags": catalog.tags_of(conn, [bag_id])[bag_id],
            "topics": catalog.topics_of(conn, bag_id),
            "all_tags": catalog.all_tags(conn),
            "bag_jobs": [j for j in jobs.list() if j.get("bag_id") == bag_id][:1],
            "roots": catalog.list_roots(conn),
            "error": error,
            "default_output": extract.default_output_path(Path(bag["path"])),
            "track_topics": track.candidates(catalog.topics_of(conn, bag_id)),
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
        sep = "&" if "?" in url else "?"
        return RedirectResponse(url + (sep + urlencode({"error": error}) if error else ""), status_code=303)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return templates.TemplateResponse(request, "jobs.html", {"jobs": jobs.list()})

    @app.post("/jobs/{job_id}/cancel", dependencies=[Depends(same_origin)])
    def cancel_job(job_id: int, next: str = Form("/jobs")):
        jobs.cancel(job_id)
        return redirect(next if next.startswith("/") and not next.startswith("//") else "/jobs")

    @app.post("/jobs/clear", dependencies=[Depends(same_origin)])
    def clear_jobs():
        jobs.clear_finished()
        return redirect("/jobs")

    # ------------------------------------------------------------ scanning

    def scan_target(roots: list[str], force: bool):
        def run(j: Job) -> dict:
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
            "roots": catalog.list_roots(conn), "error": error,
            "scan_jobs": [j for j in jobs.list() if j["kind"] == "scan" and j["state"] in ("queued", "running")],
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
        jobs.submit("scan", label, scan_target(roots, force))
        return redirect("/jobs")

    # ------------------------------------------------------------ extraction

    def result_rows(items: list[extract.BatchItem]) -> list[dict]:
        return [{
            "source": i.bag["path"], "source_id": i.bag["id"], "status": i.status,
            "message": i.message, "output": str(i.output), "result_bag_id": i.result_id,
        } for i in items]

    def submit_extract(label: str, items: list[extract.BatchItem], include: list[str], **info) -> int:
        pending = [i for i in items if i.status == "pending"]

        def run(j: Job) -> dict:
            j.update(total=1000 * len(pending))
            j.set_stats(done=0, skipped=len(items) - len(pending), failed=0, messages=0)

            def on_progress(k, n, item, frac, written):
                j.update(done=round((k + frac) * 1000), current=f"[{k + 1}/{n}] {item.bag['path']}")
                j.check_cancelled()

            def on_item(item):
                j.bump(item.status)
                j.bump("messages", item.message_count)
                j.update(results=result_rows(items))

            c = db.connect(db_path)
            try:
                extract.run_batch(c, items, include, (), on_progress, on_item)
            finally:
                c.close()
            return {"results": result_rows(items)}

        return jobs.submit("extract", label, run, reserved=[i.output for i in pending],
                           results=result_rows(items), **info)

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
        if not topic:
            return redirect(here, "select at least one topic")
        if output.strip() and not Path(output.strip()).expanduser().is_absolute():
            return redirect(here, "output path must be absolute")
        all_topics = {t["name"] for t in catalog.topics_of(conn, bag_id)}
        include = [] if set(topic) >= all_topics else topic
        items = extract.plan_batch(
            conn, [bag], start.strip() or None, end.strip() or None, include=include,
            output=Path(output.strip()).expanduser() if output.strip() else None,
            taken=jobs.reserved_outputs(),
        )
        if items[0].status == "skipped":
            return redirect(here, items[0].message)
        submit_extract(f"Extract from {bag['name']}", items, include, bag_id=bag_id)
        return redirect("/jobs")

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
            "bags": bags, "topics": topics, "error": error, "suffix": extract.default_suffix(),
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
        bags = load_bags(conn, bag)
        if not bags:
            return redirect("/", "no bags selected")
        if not topic:
            return redirect(back, "select at least one topic")
        out_dir = None
        if output_mode == "dir":
            if not output_dir.strip() or not Path(output_dir.strip()).expanduser().is_absolute():
                return redirect(back, "output folder must be an absolute path")
            out_dir = Path(output_dir.strip()).expanduser()
        suffix = suffix.strip()
        if suffix and ("/" in suffix or suffix.startswith(".")):
            return redirect(back, "suffix must not contain '/' or start with '.'")

        ids = [b["id"] for b in bags]
        all_topics = {r[0] for r in conn.execute(
            f"SELECT DISTINCT name FROM topics WHERE bag_id IN ({','.join('?' * len(ids))})", ids)}
        include = [] if set(topic) >= all_topics else topic
        items = extract.plan_batch(conn, bags, start.strip() or None, end.strip() or None, include=include,
                                   output_dir=out_dir, suffix=suffix or None, taken=jobs.reserved_outputs())
        pending = sum(i.status == "pending" for i in items)
        if not pending:
            return redirect(back, "nothing to extract: " + "; ".join(sorted({i.message for i in items})))
        submit_extract(f"Extract from {pending} bag(s)", items, include)
        return redirect("/jobs")

    # ------------------------------------------------------------ moving

    def submit_move(label: str, items: list[move.MoveItem], **info) -> int:
        pending = [i for i in items if i.status == "pending"]

        def rows() -> list[dict]:
            return [{
                "source": i.bag["path"], "source_id": i.bag["id"], "status": i.status, "message": i.message,
                "output": str(i.target), "result_bag_id": i.bag["id"] if i.status == "done" else None,
            } for i in items]

        def run(j: Job) -> dict:
            j.update(total=1000 * len(pending))
            j.set_stats(moved=0, skipped=len(items) - len(pending), failed=0)

            def on_progress(k, n, item, frac):
                j.update(done=round((k + frac) * 1000), current=f"[{k + 1}/{n}] {item.bag['path']}")
                j.check_cancelled()

            def on_item(item):
                j.bump("moved" if item.status == "done" else item.status)
                j.update(results=rows())

            c = db.connect(db_path)
            try:
                move.run_moves(c, items, on_progress, on_item)
            finally:
                c.close()
            return {"results": rows()}

        return jobs.submit("move", label, run, reserved=[i.target for i in pending], results=rows(), **info)

    @app.post("/bags/{bag_id}/move", dependencies=[Depends(same_origin)])
    def move_one(bag_id: int, dest: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
        bag = load_bag(conn, bag_id)
        here = f"/bags/{bag_id}"
        if not dest.strip() or not Path(dest.strip()).expanduser().is_absolute():
            return redirect(here, "destination must be an absolute path")
        items = move.plan_moves(conn, [bag], dest.strip(), taken=jobs.reserved_outputs())
        if items[0].status == "skipped":
            return redirect(here, items[0].message)
        submit_move(f"Move {bag['name']}", items, bag_id=bag_id)
        return redirect("/jobs")

    @app.get("/move", response_class=HTMLResponse)
    def move_page(
        request: Request,
        bag: list[int] = Query(default=[]),
        error: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        bags = load_bags(conn, bag)
        return templates.TemplateResponse(request, "move.html", {
            "bags": bags, "error": error, "roots": catalog.list_roots(conn),
            "total_size": sum(b["size_bytes"] for b in bags),
        })

    @app.post("/move", dependencies=[Depends(same_origin)])
    def move_many(bag: list[int] = Form(default=[]), dest: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn)):
        back = "/move?" + urlencode({"bag": bag}, doseq=True)
        bags = load_bags(conn, bag)
        if not bags:
            return redirect("/", "no bags selected")
        if not dest.strip() or not Path(dest.strip()).expanduser().is_absolute():
            return redirect(back, "destination must be an absolute path")
        dest_path = Path(dest.strip()).expanduser()
        if len(bags) > 1 and dest_path.exists() and not dest_path.is_dir():
            return redirect(back, f"not a directory: {dest_path}")
        # Several bags always go *into* the folder (created if needed); one bag may be renamed.
        items = move.plan_moves(conn, bags, dest_path, taken=jobs.reserved_outputs())
        pending = sum(i.status == "pending" for i in items)
        if not pending:
            return redirect(back, "nothing to move: " + "; ".join(sorted({i.message for i in items})))
        submit_move(f"Move {pending} bag(s) to {dest_path}", items)
        return redirect("/jobs")

    # ------------------------------------------------------------ importing

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request, src: str = "", dest: str = "", error: str = "",
                    conn: sqlite3.Connection = Depends(get_conn)):
        items, plan_error = [], ""
        if src.strip() and dest.strip() and not error:
            try:
                items = importer.plan_import(conn, src.strip(), dest.strip(), taken=jobs.reserved_outputs())
                if not items:
                    plan_error = f"no bags found under {src.strip()}"
            except ValueError as e:
                plan_error = str(e)
        return templates.TemplateResponse(request, "import.html", {
            "src": src, "dest": dest, "error": error or plan_error, "items": items,
            "roots": catalog.list_roots(conn),
            "need": importer.pending_bytes(items),
            "free": fileops.free_bytes(Path(dest.strip()).expanduser()) if dest.strip().startswith("/") else None,
            "pending": sum(i.status == "pending" for i in items),
        })

    @app.post("/import", dependencies=[Depends(same_origin)])
    def run_import(src: str = Form(""), dest: str = Form(""), select: list[str] = Form(default=[]),
                   conn: sqlite3.Connection = Depends(get_conn)):
        src, dest = src.strip(), dest.strip()
        back = "/import?" + urlencode({"src": src, "dest": dest})
        if not src.startswith("/") or not dest.startswith("/"):
            return redirect(back, "source and destination must be absolute paths")
        if not select:
            return redirect(back, "select at least one bag")
        try:
            items = importer.select(importer.plan_import(conn, src, dest, taken=jobs.reserved_outputs()), select)
        except ValueError as e:
            return redirect(back, str(e))
        pending = [i for i in items if i.status == "pending"]
        if not pending:
            return redirect(back, "nothing to import: the selected bags are already imported or in the way")
        need = importer.pending_bytes(items)
        if need > fileops.free_bytes(Path(dest)):
            return redirect(back, "not enough free space at the destination")
        Path(dest).mkdir(parents=True, exist_ok=True)
        catalog.add_roots(conn, [dest])

        def rows() -> list[dict]:
            return [{
                "source": str(i.source), "source_id": None, "status": i.status, "message": i.message,
                "output": str(i.target), "result_bag_id": i.result_id,
            } for i in items]

        def run(j: Job) -> dict:
            j.update(total=max(need, 1))
            j.set_stats(imported=0, skipped=len(items) - len(pending), failed=0)

            def on_progress(k, n, copied, item):
                j.update(done=copied, current=f"[{k + 1}/{n}] {item.source}")
                j.check_cancelled()

            def on_item(item):
                j.bump({"done": "imported"}.get(item.status, item.status))
                j.update(results=rows())

            c = db.connect(db_path)
            try:
                importer.run_import(c, items, on_progress, on_item)
            finally:
                c.close()
            return {"results": rows()}

        jobs.submit("import", f"Import {len(pending)} bag(s) from {src}", run,
                    reserved=[i.target for i in pending], results=rows())
        return redirect("/jobs")

    # ------------------------------------------------------------ JSON

    @app.get("/api/jobs")
    def api_jobs():
        return jobs.list()

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: int):
        j = jobs.get(job_id)
        if j is None:
            raise HTTPException(404, "job not found")
        return j

    @app.get("/api/bags")
    def api_bags(
        q: str = "",
        tag: list[str] = Query(default=[]),
        topic: str = "",
        type: str = "",
        since: str = "",
        until: str = "",
        missing: bool = False,
        root: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        rows, errors = run_search(conn, q, tag, topic, type, since, until, missing, root)
        if errors:
            raise HTTPException(400, "; ".join(errors))
        tag_map = catalog.tags_of(conn, (r["id"] for r in rows))
        return [{**dict(r), "missing": bool(r["missing"]), "tags": tag_map[r["id"]]} for r in rows]

    @app.get("/api/bags/{bag_id}/track")
    def api_track(bag_id: int, topic: str = "", conn: sqlite3.Connection = Depends(get_conn)):
        bag = load_bag(conn, bag_id)
        options = track.candidates(catalog.topics_of(conn, bag_id))
        if not options:
            raise HTTPException(404, "this bag has no supported position topic")
        if bag["missing"]:
            raise HTTPException(409, "bag files not found")
        try:
            t = track.get_track(conn, bag, topic or None)
        except Exception as e:  # unreadable data: report instead of a 500 page
            raise HTTPException(422, f"could not read the track: {type(e).__name__}: {e}")
        if t is None:
            raise HTTPException(404, f"no such position topic: {topic}")
        return {**t.to_dict(), "bag_id": bag_id, "name": bag["name"], "start_ns": bag["start_ns"],
                "candidates": options}

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
