"""Command line interface."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import click

from . import __version__, catalog, db, extract, fileops, fmt, importer, move, track


class Ctx:
    def __init__(self, db_path: str | None):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = db.connect(self.db_path)
        return self._conn

    def bag(self, ref: str) -> sqlite3.Row:
        row = catalog.get_bag(self.conn, ref)
        if row is None:
            raise click.ClickException(f"bag not found in catalog: {ref}")
        return row


pass_ctx = click.make_pass_decorator(Ctx)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--db", "db_path", envvar="BAGARIUM_DB", type=click.Path(dir_okay=False),
              help=f"Catalog database (default: {db.default_db_path()}).")
@click.version_option(__version__)
@click.pass_context
def main(ctx: click.Context, db_path: str | None) -> None:
    """bagarium - a local catalog for ROS 2 bags (mcap / sqlite3)."""
    ctx.obj = Ctx(db_path)


@main.command()
@click.argument("paths", nargs=-1, type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Re-read bags even if they look unchanged.")
@click.option("--no-register", is_flag=True, help="Do not add PATHS to the registered roots.")
@click.option("-v", "--verbose", is_flag=True, help="Print every bag.")
@pass_ctx
def scan(c: Ctx, paths: tuple[Path, ...], force: bool, no_register: bool, verbose: bool) -> None:
    """Find bags under PATHS and add/update them in the catalog.

    Directories given as PATHS are registered as roots (see `bagarium roots`).
    Without PATHS, all registered roots are scanned.
    """
    if not paths:
        paths = tuple(Path(r["path"]) for r in catalog.list_roots(c.conn))
        if not paths:
            raise click.UsageError("no PATHS given and no roots registered")
    elif not no_register:
        catalog.add_roots(c.conn, [p for p in paths if p.is_dir()])

    def on_bag(path: Path, status: str, done: int, total: int) -> None:
        if verbose or status not in ("unchanged",):
            color = {"added": "green", "updated": "yellow", "moved": "cyan", "error": "red"}.get(status)
            click.echo(f"{click.style(f'{status:>9}', fg=color)}  [{done}/{total}] {path}")

    result = catalog.scan(c.conn, paths, force=force, on_bag=on_bag)
    for path, message in result.errors:
        click.secho(f"error: {path}: {message}", fg="red", err=True)
    click.echo(
        f"added {result.added}, updated {result.updated}, moved {result.moved}, "
        f"unchanged {result.unchanged}, missing {result.missing}, errors {len(result.errors)}"
    )


@main.group(invoke_without_command=True)
@click.pass_context
def roots(ctx: click.Context) -> None:
    """List registered roots (directories scanned by `bagarium scan` without PATHS)."""
    if ctx.invoked_subcommand:
        return
    c: Ctx = ctx.obj
    rows = catalog.list_roots(c.conn)
    if not rows:
        click.echo("no roots registered", err=True)
    for r in rows:
        status = "" if r["exists"] else click.style("  [not found]", fg="red")
        click.echo(f"{r['bag_count']:>5} bags  last scan {r['last_scanned_at'] or 'never':<25}  {r['path']}{status}")


@roots.command(name="add")
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path))
@pass_ctx
def roots_add(c: Ctx, paths: tuple[Path, ...]) -> None:
    """Register directories as roots (does not scan)."""
    try:
        for path in catalog.add_roots(c.conn, paths):
            click.echo(f"added {path}")
    except ValueError as e:
        raise click.ClickException(str(e))


@roots.command(name="rm")
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path))
@pass_ctx
def roots_rm(c: Ctx, paths: tuple[Path, ...]) -> None:
    """Unregister roots. Indexed bags, tags and notes are kept."""
    click.echo(f"removed {catalog.remove_roots(c.conn, paths)} root(s)")


def _time_option(name: str, end: bool):
    def convert(ctx, param, value):
        if value is None:
            return None
        try:
            return catalog.parse_time(value, end_of_day=end)
        except ValueError:
            raise click.BadParameter("use YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS]")

    return click.option(name, callback=convert, metavar="TIME",
                        help=("Bags recorded before this time (a bare date includes the whole day)."
                              if end else "Bags recorded at/after this time."))


@main.command(name="ls")
@click.argument("query", nargs=-1)
@click.option("-t", "--tag", "tags", multiple=True, help="Require tag (repeatable).")
@click.option("--topic", "topics", multiple=True, help="Require a topic name (substring or glob, repeatable).")
@click.option("--type", "types", multiple=True, help="Require a message type (substring or glob, repeatable).")
@_time_option("--since", end=False)
@_time_option("--until", end=True)
@click.option("-r", "--root", type=click.Path(file_okay=False, path_type=Path),
              help="Only bags at or under this directory (e.g. a registered root).")
@click.option("--missing", is_flag=True, help="Include bags whose files are gone.")
@click.option("-n", "--limit", type=int, help="Show at most N bags.")
@click.option("--paths", "paths_only", is_flag=True, help="Print only paths (for piping).")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@pass_ctx
def ls(c: Ctx, query, tags, topics, types, since, until, root, missing, limit, paths_only, as_json) -> None:
    """List / search bags. QUERY words match name, path and note."""
    rows = catalog.search(c.conn, " ".join(query), tags, topics, types, since, until, missing, limit, root=root)
    tag_map = catalog.tags_of(c.conn, (r["id"] for r in rows))

    if paths_only:
        for r in rows:
            click.echo(r["path"])
        return
    if as_json:
        click.echo(json.dumps([_bag_dict(r, tag_map[r["id"]]) for r in rows], ensure_ascii=False, indent=2))
        return
    if not rows:
        click.echo("no bags", err=True)
        return

    table = [("ID", "START", "DURATION", "SIZE", "MSGS", "NAME", "TAGS")]
    for r in rows:
        name = r["name"] + (" [missing]" if r["missing"] else "") + (" [error]" if r["error"] else "")
        table.append((str(r["id"]), fmt.time(r["start_ns"]), fmt.duration(r["start_ns"], r["end_ns"]),
                      fmt.size(r["size_bytes"]), fmt.count(r["message_count"]), name, ",".join(tag_map[r["id"]])))
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]) - 1)]
    for i, row in enumerate(table):
        line = "  ".join(cell.ljust(w) for cell, w in zip(row, widths)) + "  " + row[-1]
        click.echo(click.style(line, bold=True) if i == 0 else line.rstrip())


@main.command()
@click.argument("bag")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@pass_ctx
def show(c: Ctx, bag: str, as_json: bool) -> None:
    """Show details of BAG (id or path)."""
    r = c.bag(bag)
    tags = catalog.tags_of(c.conn, [r["id"]])[r["id"]]
    topics = catalog.topics_of(c.conn, r["id"])
    if as_json:
        d = _bag_dict(r, tags)
        d["topics"] = [dict(t) for t in topics]
        click.echo(json.dumps(d, ensure_ascii=False, indent=2))
        return

    fields = [
        ("id", r["id"]), ("path", r["path"]), ("storage", r["storage"]),
        ("start", fmt.time(r["start_ns"])), ("end", fmt.time(r["end_ns"])),
        ("duration", fmt.duration(r["start_ns"], r["end_ns"])), ("size", fmt.size(r["size_bytes"])),
        ("messages", fmt.count(r["message_count"])), ("tags", ", ".join(tags) or "-"),
    ]
    if r["missing"]:
        fields.append(("status", click.style("missing (files not found at last scan)", fg="red")))
    if r["error"]:
        fields.append(("error", click.style(r["error"], fg="red")))
    for key, value in fields:
        click.echo(f"{key:>9}  {value}")
    if r["note"]:
        click.echo("     note  " + r["note"].replace("\n", "\n" + " " * 11))
    if topics:
        click.echo()
        w_name = max(len(t["name"]) for t in topics)
        w_type = max(len(t["type"]) for t in topics)
        for t in topics:
            click.echo(f"  {t['name'].ljust(w_name)}  {t['type'].ljust(w_type)}  {fmt.count(t['message_count']):>10}")


@main.command()
@click.argument("bag")
@click.argument("tags", nargs=-1, required=True)
@pass_ctx
def tag(c: Ctx, bag: str, tags: tuple[str, ...]) -> None:
    """Add TAGS to BAG."""
    catalog.add_tags(c.conn, c.bag(bag)["id"], tags)


@main.command()
@click.argument("bag")
@click.argument("tags", nargs=-1, required=True)
@pass_ctx
def untag(c: Ctx, bag: str, tags: tuple[str, ...]) -> None:
    """Remove TAGS from BAG."""
    catalog.remove_tags(c.conn, c.bag(bag)["id"], tags)


@main.command()
@click.argument("bag")
@click.argument("text", required=False)
@click.option("--clear", is_flag=True, help="Remove the note.")
@pass_ctx
def note(c: Ctx, bag: str, text: str | None, clear: bool) -> None:
    """Set the note of BAG. Without TEXT, opens $EDITOR."""
    r = c.bag(bag)
    if clear:
        text = ""
    elif text is None:
        text = click.edit(r["note"] or "")
        if text is None:
            click.echo("note unchanged", err=True)
            return
    catalog.set_note(c.conn, r["id"], text)


@main.command(name="tags")
@pass_ctx
def list_tags(c: Ctx) -> None:
    """List all tags with bag counts."""
    for row in catalog.all_tags(c.conn):
        click.echo(f"{row['count']:>5}  {row['name']}")


@main.command(name="forget")
@click.argument("bags", nargs=-1, required=True)
@pass_ctx
def forget_cmd(c: Ctx, bags: tuple[str, ...]) -> None:
    """Remove BAGS from the catalog. Files on disk are not touched."""
    n = catalog.forget(c.conn, [c.bag(b)["id"] for b in bags])
    click.echo(f"forgot {n} bag(s)")


@main.command()
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_ctx
def prune(c: Ctx, yes: bool) -> None:
    """Remove bags marked missing (with their tags and notes) from the catalog."""
    ids = catalog.missing_ids(c.conn)
    if not ids:
        click.echo("nothing to prune")
        return
    if not yes:
        click.confirm(f"forget {len(ids)} missing bag(s) including their tags and notes?", abort=True)
    click.echo(f"forgot {catalog.forget(c.conn, ids)} bag(s)")


@main.command(name="extract")
@click.argument("bags", nargs=-1, required=True)
@click.option("-o", "--output", type=click.Path(path_type=Path),
              help="Output bag directory (single BAG only).")
@click.option("-O", "--output-dir", type=click.Path(file_okay=False, path_type=Path),
              help="Put outputs in this directory (default: next to each source bag).")
@click.option("--suffix", help="Output name suffix: <bag>_<suffix> (default: extract_<timestamp>).")
@click.option("-s", "--start", metavar="TIME",
              help="Start: seconds from each bag's start (90, 1:30) or a time (2026-09-02T09:30:00).")
@click.option("-e", "--end", metavar="TIME", help="End, same format as --start.")
@click.option("-d", "--duration", type=float, metavar="SECONDS", help="Length from --start (instead of --end).")
@click.option("-t", "--topic", "topics", multiple=True, metavar="PATTERN",
              help="Keep topics matching this name or glob (repeatable; default: all).")
@click.option("-x", "--exclude", multiple=True, metavar="PATTERN", help="Drop topics matching this name or glob (repeatable).")
@click.option("--no-index", is_flag=True, help="Do not add the results to the catalog.")
@pass_ctx
def extract_cmd(c: Ctx, bags, output, output_dir, suffix, start, end, duration, topics, exclude, no_index) -> None:
    """Write a time range and/or a subset of topics of each BAG to a new bag.

    BAGS are ids or paths; select them with a search, e.g.
    `bagarium extract -t '/sensing/*' $(bagarium ls -t outdoor --paths)`.
    Each new bag has the source's storage format (sqlite3 / mcap) and can be
    played with `ros2 bag play`. Split and compressed bags are supported.
    """
    if end and duration is not None:
        raise click.UsageError("use either --end or --duration")
    if output and (len(bags) > 1 or output_dir):
        raise click.UsageError("--output takes a single BAG; use --output-dir for several")
    rows = list({r["id"]: r for r in (c.bag(b) for b in bags)}.values())
    items = extract.plan_batch(c.conn, rows, start, end, duration, topics, exclude, output_dir, suffix, output)

    if len(items) == 1 and items[0].status == "skipped":
        raise click.ClickException(items[0].message)
    for item in items:
        if item.status == "skipped":
            click.secho(f"   skipped  {item.bag['path']}: {item.message}", fg="yellow")
    todo = [i for i in items if i.status == "pending"]
    if not todo:
        raise click.ClickException("nothing to extract")

    with click.progressbar(length=1000 * len(todo), label=f"extracting {len(todo)} bag(s)", show_percent=True) as bar:
        def on_progress(k: int, n: int, item, frac: float, written: int) -> None:
            bar.update(round((k + frac) * 1000) - bar.pos)

        extract.run_batch(c.conn, items, topics, exclude, on_progress, index=not no_index)

    for item in todo:
        if item.status == "done":
            ref = f" (#{item.result_id})" if item.result_id is not None else ""
            click.echo(f"{click.style('     wrote', fg='green')}  {item.output}{ref}: {fmt.count(item.message_count)} messages")
        else:
            click.secho(f"    failed  {item.bag['path']}: {item.message}", fg="red")
    counts = {s: sum(i.status == s for i in items) for s in ("done", "skipped", "failed")}
    click.echo(f"done {counts['done']}, skipped {counts['skipped']}, failed {counts['failed']}")
    if counts["failed"]:
        raise SystemExit(1)


@main.command(name="mv")
@click.argument("bags", nargs=-1, required=True)
@click.argument("dest", type=click.Path(path_type=Path))
@click.option("-n", "--dry-run", is_flag=True, help="Show what would be moved without moving anything.")
@pass_ctx
def mv_cmd(c: Ctx, bags: tuple[str, ...], dest: Path, dry_run: bool) -> None:
    """Move BAGS into the directory DEST (or rename a single BAG to DEST).

    Tags, notes and ids are kept. Bags nested inside a moved bag directory
    move with it. Across filesystems the bag is copied and checked before the
    source is removed.
    """
    rows = list({r["id"]: r for r in (c.bag(b) for b in bags)}.values())
    if len(rows) > 1 and dest.exists() and not dest.is_dir():
        raise click.UsageError(f"not a directory: {dest}")
    items = move.plan_moves(c.conn, rows, dest)
    if len(items) == 1 and items[0].status == "skipped":
        raise click.ClickException(items[0].message)
    for item in items:
        if item.status == "skipped":
            click.secho(f"   skipped  {item.bag['path']}: {item.message}", fg="yellow")
        elif dry_run:
            click.echo(f"     would  {item.bag['path']} -> {item.target}")
    todo = [i for i in items if i.status == "pending"]
    if not todo:
        raise click.ClickException("nothing to move")
    if dry_run:
        return

    with click.progressbar(length=1000 * len(todo), label=f"moving {len(todo)} bag(s)", show_percent=True) as bar:
        def on_progress(k: int, n: int, item, frac: float) -> None:
            bar.update(round((k + frac) * 1000) - bar.pos)

        move.run_moves(c.conn, items, on_progress)

    for item in todo:
        if item.status == "done":
            click.echo(f"{click.style('     moved', fg='green')}  {item.bag['path']} -> {item.target}")
            if item.message:
                click.secho(f"   warning  {item.message}", fg="yellow")
        else:
            click.secho(f"    failed  {item.bag['path']}: {item.message}", fg="red")
    counts = {s: sum(i.status == s for i in items) for s in ("done", "skipped", "failed")}
    click.echo(f"moved {counts['done']}, skipped {counts['skipped']}, failed {counts['failed']}")
    if counts["done"] and not all(catalog.under_root(c.conn, i.target) for i in todo if i.status == "done"):
        click.secho("note: some destinations are outside the registered roots, so scans will not revisit them "
                    "(see `bagarium roots add`)", fg="yellow")
    if counts["failed"]:
        raise SystemExit(1)


@main.command(name="import")
@click.argument("src", type=click.Path(exists=True, path_type=Path))
@click.argument("dest", type=click.Path(file_okay=False, path_type=Path))
@click.option("-n", "--dry-run", is_flag=True, help="Show what would be copied without copying.")
@click.option("-i", "--include", multiple=True, metavar="PATTERN",
              help="Only bags whose path relative to SRC matches this glob (repeatable).")
@click.option("-x", "--exclude", multiple=True, metavar="PATTERN",
              help="Skip bags whose path relative to SRC matches this glob (repeatable).")
@click.option("--no-register", is_flag=True, help="Do not register DEST as a root.")
@pass_ctx
def import_cmd(c: Ctx, src: Path, dest: Path, dry_run: bool, include, exclude, no_register: bool) -> None:
    """Copy the bags under SRC (e.g. an SSD) into DEST and add them to the catalog.

    The folder layout below SRC is kept and SRC is never modified. Bags that
    are already in the catalog (same time range, message count and size) are
    skipped, so running the same import again only copies new bags.
    Use --include / --exclude to pick bags, e.g. `-i '2026-09-*/*' -x '*/derivatives/*'`.
    """
    try:
        items = importer.plan_import(c.conn, src, dest, include=include, exclude=exclude)
    except ValueError as e:
        raise click.ClickException(str(e))
    if not items:
        raise click.ClickException(f"no bags found under {src}" + (" matching the patterns" if include or exclude else ""))

    for item in items:
        if item.status == "skipped":
            click.secho(f"   skipped  {item.source}: {item.message}", fg="yellow")
        elif dry_run:
            click.echo(f"      copy  {item.source} -> {item.target}  ({fmt.size(item.size)})")
    todo = [i for i in items if i.status == "pending"]
    need, free = importer.pending_bytes(items), fileops.free_bytes(Path(dest).expanduser())
    click.echo(f"{len(todo)} bag(s) to copy, {fmt.size(need)}; {fmt.size(free)} free at destination")
    if not todo:
        return
    if need > free:
        raise click.ClickException("not enough free space at the destination")
    if dry_run:
        return
    if not no_register:
        Path(dest).expanduser().mkdir(parents=True, exist_ok=True)
        catalog.add_roots(c.conn, [dest])

    with click.progressbar(length=max(need, 1), label=f"copying {len(todo)} bag(s)", show_percent=True) as bar:
        def on_progress(k: int, n: int, copied: int, item) -> None:
            bar.update(copied - bar.pos)

        importer.run_import(c.conn, items, on_progress)

    for item in todo:
        if item.status == "done":
            click.echo(f"{click.style('  imported', fg='green')}  {item.target} (#{item.result_id})")
        else:
            click.secho(f"    {item.status:>6}  {item.source}: {item.message}", fg="red" if item.status == "failed" else "yellow")
    counts = {s: sum(i.status == s for i in items) for s in ("done", "skipped", "failed")}
    click.echo(f"imported {counts['done']}, skipped {counts['skipped']}, failed {counts['failed']}")
    if counts["failed"]:
        raise SystemExit(1)


@main.command(name="track")
@click.argument("bags", nargs=-1, required=True)
@click.option("--topic", help="Position topic (default: the best supported one in each bag).")
@click.option("--format", "fmt_", type=click.Choice(["geojson", "csv"]), default="geojson", show_default=True)
@pass_ctx
def track_cmd(c: Ctx, bags: tuple[str, ...], topic: str | None, fmt_: str) -> None:
    """Print the driving track (latitude / longitude) of BAGS as GeoJSON or CSV.

    Read from VehicleKinematics, NavSatFix or GeoPoint/GeoPoseStamped topics;
    points are thinned out to about 0.5 m spacing.
    """
    features = []
    if fmt_ == "csv":
        click.echo("bag,topic,offset_s,time,latitude,longitude")
    for ref in bags:
        r = c.bag(ref)
        try:
            t = track.get_track(c.conn, r, topic)
        except Exception as e:
            click.secho(f"error: {r['path']}: {type(e).__name__}: {e}", fg="red", err=True)
            continue
        if t is None:
            click.secho(f"skipped: {r['path']}: no supported position topic", fg="yellow", err=True)
            continue
        if fmt_ == "csv":
            for lat, lon, off in t.points:
                ts = fmt.time(r["start_ns"] + round(off * 1e9)) if r["start_ns"] is not None else ""
                click.echo(f"{r['path']},{t.topic},{off},{ts},{lat},{lon}")
        else:
            features.append(track.to_geojson(t, r))
    if fmt_ == "geojson":
        click.echo(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False))


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@pass_ctx
def serve(c: Ctx, host: str, port: int) -> None:
    """Start the web UI."""
    import uvicorn

    from .web import create_app

    click.echo(f"bagarium web UI: http://{host}:{port}/")
    uvicorn.run(create_app(c.db_path), host=host, port=port, log_level="warning")


def _bag_dict(r: sqlite3.Row, tags: list[str]) -> dict:
    d = dict(r)
    d["missing"] = bool(d["missing"])
    d["tags"] = tags
    return d
