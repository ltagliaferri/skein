"""Daily-driver CLI for the content-hash station (Slice 3).

A direct, local CLI over :class:`skein_next.station.Station` — no server, no HTTP
hop. It serves the daily verbs against the ``.skein-next/`` data dir, beside the
legacy ``skein`` command which is never touched:

    post    create a folio in a site
    folio   read one folio
    folios  list a site's folios
    search  substring search over title/content
    thread  walk a folio's thread graph
    sites   list sites
    site    create a site (so a fresh station is usable)
    import  import a legacy SKEIN project into this station (read-only on source)
    verify  re-check the fidelity invariants of an import report

Output is plain text built for a screen reader: one item per line, a human
description followed by the id used to look it up — no tables, no columns, no
markdown. ``--json`` on the read/list verbs emits structured data for tooling.

Run via the ``interskein`` console script or ``python -m skein_next``.
"""

from __future__ import annotations

import json as _json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from .bridge import ImportReport, import_project
from .station import (
    AmbiguousReference,
    Station,
    UnknownFolio,
    UnknownSite,
    _short,
    _title_line,
)

ENV_DATA_DIR = "SKEIN_NEXT_DATA_DIR"
ENV_AGENT = "SKEIN_NEXT_AGENT"


def _default_author() -> Optional[str]:
    return os.environ.get(ENV_AGENT) or os.environ.get("SKEIN_AGENT")


def _open_station(ctx: click.Context) -> Station:
    return Station(ctx.obj.get("data_dir"))


def _folio_line(folio: Dict[str, Any]) -> str:
    """One screen-reader line for a folio: ``<type> <title>  <short-id>``."""
    ftype = folio.get("type") or "folio"
    title = _title_line(folio.get("title")) or _title_line(folio.get("content")) or "(untitled)"
    return f"{ftype} {title}  {_short(folio['content_hash'])}"


def _emit_json(payload: Any) -> None:
    click.echo(_json.dumps(payload, indent=2, ensure_ascii=False, default=str))


# --- import fidelity gate ---------------------------------------------------
#
# Four invariants must hold for an import to be faithful. Everything else the
# report counts — threads merged, actor endpoints folded/kept, unresolved refs,
# status-without-thread, the legacy columns the new schema intentionally omits
# (the accepted pure-threads + v0 multi-actor-thread drops, finding-20260529-y2d6)
# — is EXPECTED, not a failure. Those are surfaced by ``ImportReport.render()``
# so they are visible rather than silent; only these four gate the exit code.


def _fidelity_failures(report: ImportReport) -> List[Tuple[str, str]]:
    """Return failed hard invariants as ``(invariant, offending counts)`` pairs."""
    failures: List[Tuple[str, str]] = []
    if report.folios_carried != report.folios_seen:
        failures.append((
            "folios_carried == folios_seen",
            f"carried {report.folios_carried} of {report.folios_seen} seen",
        ))
    if report.folio_hash_collisions != 0:
        failures.append((
            "folio_hash_collisions == 0",
            f"{report.folio_hash_collisions} collisions",
        ))
    if report.actor_endpoints_dropped != 0:
        failures.append((
            "actor_endpoints_dropped == 0",
            f"{report.actor_endpoints_dropped} dropped: {report.dropped_examples}",
        ))
    if report.sites_carried != report.sites_seen:
        failures.append((
            "sites_carried == sites_seen",
            f"carried {report.sites_carried} of {report.sites_seen} seen",
        ))
    return failures


def _emit_fidelity(report: ImportReport) -> bool:
    """Print the fidelity verdict. Return True when every invariant holds."""
    failures = _fidelity_failures(report)
    if not failures:
        click.echo("FIDELITY OK")
        return True
    click.echo("FIDELITY FAILED")
    for invariant, detail in failures:
        click.echo(f"  invariant {invariant} violated: {detail}")
    return False


def _resolve_legacy_paths(
    project_root: Optional[str],
    legacy_db: Optional[str],
    sites_dir: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Derive ``(db_path, sites_dir)`` for a legacy project, honoring overrides.

    A legacy project root P holds ``P/.skein/data/skein.db`` and
    ``P/.skein/data/sites/``. ``--legacy-db`` / ``--sites-dir`` override either
    path independently. Returns ``(None, None)`` when neither a project root nor
    both overrides are supplied.
    """
    db, sd = legacy_db, sites_dir
    if project_root:
        base = Path(project_root) / ".skein" / "data"
        if db is None:
            db = str(base / "skein.db")
        if sd is None:
            sd = str(base / "sites")
    if db is None or sd is None:
        return (None, None)
    return (db, sd)


def _run_import(
    ctx: click.Context,
    project_root: Optional[str],
    legacy_db: Optional[str],
    sites_dir: Optional[str],
) -> ImportReport:
    """Resolve paths, open the target station, and run the read-only import."""
    db_path, sd_path = _resolve_legacy_paths(project_root, legacy_db, sites_dir)
    if db_path is None:
        raise click.ClickException(
            "give a PROJECT_ROOT, or both --legacy-db and --sites-dir."
        )
    if not Path(db_path).is_file():
        raise click.ClickException(f"no legacy database at {db_path}")
    with _open_station(ctx) as station:
        return import_project(db_path, sd_path, station.store)


@click.group()
@click.option(
    "--data-dir",
    default=None,
    help=f"Station data dir (default: $.{ENV_DATA_DIR} or ./.skein-next).",
)
@click.pass_context
def cli(ctx: click.Context, data_dir: Optional[str]) -> None:
    """new-skein: the local content-hash station."""
    ctx.ensure_object(dict)
    ctx.obj["data_dir"] = data_dir or os.environ.get(ENV_DATA_DIR)


# --- post -------------------------------------------------------------------


@cli.command()
@click.argument("type")
@click.argument("site")
@click.argument("title")
@click.option("--content", "-c", default=None, help="Folio body. '-' reads stdin.")
@click.option("--by", "created_by", default=None, help="Author (default: $SKEIN_NEXT_AGENT).")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def post(
    ctx: click.Context,
    type: str,
    site: str,
    title: str,
    content: Optional[str],
    created_by: Optional[str],
    output_json: bool,
) -> None:
    """Create a TYPE folio titled TITLE in SITE.

    Examples: interskein post finding myproj "Cache key collides under load"
    """
    if content == "-":
        content = sys.stdin.read()
    author = created_by or _default_author()
    with _open_station(ctx) as station:
        try:
            folio_hash = station.post(
                type=type, site=site, title=title, content=content, created_by=author
            )
        except UnknownSite as e:
            raise click.ClickException(str(e) + " — create it first with 'site create'.")
        if output_json:
            _emit_json(station.store.get_folio(folio_hash))
        else:
            click.echo(folio_hash)


# --- folio (read) -----------------------------------------------------------


@cli.command()
@click.argument("ref")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def folio(ctx: click.Context, ref: str, output_json: bool) -> None:
    """Read one folio by its hash, a short prefix, or a legacy id."""
    with _open_station(ctx) as station:
        try:
            found = station.get_folio(ref)
        except AmbiguousReference as e:
            lines = "\n".join("  " + _short(m, 24) for m in e.matches)
            raise click.ClickException(f"{e}\n{lines}")
        if not found:
            raise click.ClickException(f"no folio for reference {ref!r}")
        status = station.status_of(found["content_hash"])
        if output_json:
            _emit_json({**found, "status": status})
            return
        click.echo(f"{found.get('type') or 'folio'}  {found['content_hash']}")
        if found.get("title"):
            click.echo(found["title"])
        meta = []
        if found.get("created_by"):
            meta.append(f"by {found['created_by']}")
        if found.get("created_at"):
            meta.append(found["created_at"])
        if status:
            meta.append(f"status: {status}")
        if meta:
            click.echo(" - ".join(meta))
        if found.get("content"):
            click.echo("")
            click.echo(found["content"])


# --- folios (list a site) ---------------------------------------------------


@cli.command()
@click.argument("site")
@click.option("--type", "-t", "type_filter", default=None, help="Only this folio type.")
@click.option("-n", "--limit", type=int, default=100, help="Max folios shown.")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def folios(
    ctx: click.Context,
    site: str,
    type_filter: Optional[str],
    limit: int,
    output_json: bool,
) -> None:
    """List the folios in SITE."""
    with _open_station(ctx) as station:
        try:
            items = station.folios_in_site(site, type=type_filter, limit=limit)
        except UnknownSite as e:
            raise click.ClickException(str(e))
        if output_json:
            _emit_json(items)
            return
        if not items:
            click.echo(f"(no folios in {site})")
            return
        for f in items:
            click.echo(_folio_line(f))


# --- search -----------------------------------------------------------------


@cli.command()
@click.argument("query")
@click.option("-n", "--limit", type=int, default=50, help="Max results.")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def search(ctx: click.Context, query: str, limit: int, output_json: bool) -> None:
    """Find folios whose title or content contains QUERY."""
    with _open_station(ctx) as station:
        items = station.search(query, limit=limit)
        if output_json:
            _emit_json(items)
            return
        if not items:
            click.echo(f"(no folios match {query!r})")
            return
        for f in items:
            click.echo(_folio_line(f))


# --- thread (graph) ---------------------------------------------------------


def _peer_label(peer: Dict[str, Any]) -> str:
    if peer["kind"] == "folio":
        return _folio_line(peer["folio"])
    if peer["kind"] == "ref":
        return f"(unresolved) {peer['id']}"
    return "(none)"


@cli.command()
@click.argument("ref")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def thread(ctx: click.Context, ref: str, output_json: bool) -> None:
    """Show the threads into and out of a folio."""
    with _open_station(ctx) as station:
        try:
            graph = station.thread_graph(ref)
        except AmbiguousReference as e:
            lines = "\n".join("  " + _short(m, 24) for m in e.matches)
            raise click.ClickException(f"{e}\n{lines}")
        if not graph:
            raise click.ClickException(f"no folio for reference {ref!r}")
        if output_json:
            _emit_json(graph)
            return
        focus = graph["folio"]
        click.echo(_folio_line(focus) if focus else graph["content_hash"])
        for edge in graph["outgoing"]:
            click.echo(f"  links to ({edge['type']}): {_peer_label(edge['peer'])}")
        for edge in graph["incoming"]:
            click.echo(f"  linked from ({edge['type']}): {_peer_label(edge['peer'])}")
        for edge in graph["memberships"]:
            # A within edge points member -> site. Which end the focus is on
            # flips the relationship: if the focus is the member, the peer is its
            # site ("within"); if the focus is the site, the peer is a member it
            # "contains". Printing "within" unconditionally states it backwards
            # when you run `thread` on a site.
            if edge["from_id"] == graph["content_hash"]:
                click.echo(f"  within {_peer_label(edge['peer'])}")
            else:
                click.echo(f"  contains {_peer_label(edge['peer'])}")
        if not (graph["outgoing"] or graph["incoming"] or graph["memberships"]):
            click.echo("  (no threads)")


# --- status / close ---------------------------------------------------------


def _set_status(ctx: click.Context, ref: str, value: str, created_by: Optional[str]) -> None:
    author = created_by or _default_author()
    with _open_station(ctx) as station:
        try:
            folio_hash = station.set_status(ref, value, by=author)
        except AmbiguousReference as e:
            lines = "\n".join("  " + _short(m, 24) for m in e.matches)
            raise click.ClickException(f"{e}\n{lines}")
        except UnknownFolio as e:
            raise click.ClickException(str(e))
        click.echo(f"status set: {value}  {_short(folio_hash)}")


@cli.command()
@click.argument("ref")
@click.argument("value")
@click.option("--by", "created_by", default=None, help="Author (default: $SKEIN_NEXT_AGENT).")
@click.pass_context
def status(ctx: click.Context, ref: str, value: str, created_by: Optional[str]) -> None:
    """Set a folio's status (writes a status thread; latest wins)."""
    _set_status(ctx, ref, value, created_by)


@cli.command()
@click.argument("ref")
@click.option("--by", "created_by", default=None, help="Author (default: $SKEIN_NEXT_AGENT).")
@click.pass_context
def close(ctx: click.Context, ref: str, created_by: Optional[str]) -> None:
    """Close a folio (shorthand for 'status REF closed')."""
    _set_status(ctx, ref, "closed", created_by)


# --- sites ------------------------------------------------------------------


@cli.command()
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def sites(ctx: click.Context, output_json: bool) -> None:
    """List the sites in this station."""
    with _open_station(ctx) as station:
        pairs = station.list_sites()
        if output_json:
            _emit_json(
                [{"slug": slug, "folio": folio} for slug, folio in pairs]
            )
            return
        if not pairs:
            click.echo("(no sites)")
            return
        for slug, folio in pairs:
            purpose = _title_line(folio.get("content")) if folio else ""
            click.echo(f"{slug} - {purpose}" if purpose else slug)


@cli.command(name="site")
@click.argument("action", type=click.Choice(["create"]))
@click.argument("slug")
@click.option("--purpose", "-p", default=None, help="What the site is for.")
@click.option("--by", "created_by", default=None, help="Author.")
@click.pass_context
def site(
    ctx: click.Context,
    action: str,
    slug: str,
    purpose: Optional[str],
    created_by: Optional[str],
) -> None:
    """Manage sites. Currently: site create SLUG."""
    author = created_by or _default_author()
    with _open_station(ctx) as station:
        existing = station.store.resolve_slug(slug)
        site_hash = station.create_site(slug, purpose=purpose, created_by=author)
        verb = "exists" if existing == site_hash else "created"
        click.echo(f"site {verb}: {slug}  {site_hash}")


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind host.")
@click.option("--port", default=9001, type=int, help="Bind port (default 9001).")
@click.pass_context
def serve(ctx: click.Context, host: str, port: int) -> None:
    """Serve the read-only web surface (default http://127.0.0.1:9001).

    The web app reads the same station data dir; pass it via --data-dir or
    $SKEIN_NEXT_DATA_DIR. FastAPI/uvicorn are imported only when this runs.
    """
    data_dir = ctx.obj.get("data_dir")
    if data_dir:
        os.environ[ENV_DATA_DIR] = data_dir
    from .web.app import run_server  # lazy: keep the heavy web deps off other verbs

    run_server(host=host, port=port)


# --- import / verify --------------------------------------------------------


@cli.command(name="import")
@click.argument("project_root", required=False, type=click.Path())
@click.option(
    "--legacy-db",
    "legacy_db",
    default=None,
    type=click.Path(),
    help="Legacy skein.db (default: PROJECT_ROOT/.skein/data/skein.db).",
)
@click.option(
    "--sites-dir",
    "sites_dir",
    default=None,
    type=click.Path(),
    help="Legacy sites dir (default: PROJECT_ROOT/.skein/data/sites).",
)
@click.option(
    "--verify",
    is_flag=True,
    help="Assert the fidelity invariants after import; exit non-zero on any failure.",
)
@click.pass_context
def import_(
    ctx: click.Context,
    project_root: Optional[str],
    legacy_db: Optional[str],
    sites_dir: Optional[str],
    verify: bool,
) -> None:
    """Import a legacy SKEIN project into this station (read-only on the source).

    PROJECT_ROOT is a legacy project dir holding .skein/data/skein.db and
    .skein/data/sites/. Override either path with --legacy-db / --sites-dir. The
    target store is the global --data-dir. Idempotent: re-importing into the same
    data dir is safe.

    Examples: interskein import /home/patrick/projects/tome --verify
    """
    report = _run_import(ctx, project_root, legacy_db, sites_dir)
    click.echo(report.render())
    if verify:
        if not _emit_fidelity(report):
            ctx.exit(1)


@cli.command()
@click.argument("project_root", required=False, type=click.Path())
@click.option(
    "--legacy-db",
    "legacy_db",
    default=None,
    type=click.Path(),
    help="Legacy skein.db (default: PROJECT_ROOT/.skein/data/skein.db).",
)
@click.option(
    "--sites-dir",
    "sites_dir",
    default=None,
    type=click.Path(),
    help="Legacy sites dir (default: PROJECT_ROOT/.skein/data/sites).",
)
@click.pass_context
def verify(
    ctx: click.Context,
    project_root: Optional[str],
    legacy_db: Optional[str],
    sites_dir: Optional[str],
) -> None:
    """Re-derive an import report and check the fidelity invariants.

    Re-runs the (idempotent, read-only) import to recompute the report, prints
    it, then asserts the four hard invariants. Exits non-zero if any fails.
    """
    report = _run_import(ctx, project_root, legacy_db, sites_dir)
    click.echo(report.render())
    if not _emit_fidelity(report):
        ctx.exit(1)


def main() -> None:
    """Entry point for the ``interskein`` console script."""
    cli(prog_name="interskein")


if __name__ == "__main__":
    main()
