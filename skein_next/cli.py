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
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import click

from .bridge import ImportReport, import_project
from . import publish as _publish
from .publish import PublishError
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


def _build_signer(
    oidc_issuer: str,
    oidc_token: Optional[str],
    login: bool = False,
    force_oob: bool = False,
):
    """Construct a boundary signer (the ceremony seam).

    Two ways to get the identity: ``--login`` runs the interactive Sigstore flow
    here (opens a browser, or prints a code with ``--oob`` for SSH/headless), or
    ``--oidc-token`` wraps a token the operator obtained out of band ('-' reads
    stdin). The token is short-lived, so login-then-publish must be prompt.
    """
    from . import sign as _sign  # lazy: keep sigstore off the other verbs

    if login:
        provider = _sign.acquire_oidc_provider(force_oob=force_oob)
        return _sign.make_oidc_signer(provider)

    if oidc_token == "-":
        oidc_token = sys.stdin.read().strip()
    if not oidc_token:
        raise click.ClickException(
            "--sign needs --login (interactive) or --oidc-token (or '-' for stdin)."
        )
    from skein.signing import OIDCProviderConfig

    provider = OIDCProviderConfig(issuer=oidc_issuer, token=oidc_token, provider_id=None)
    return _sign.make_oidc_signer(provider)


def _folio_line(folio: Dict[str, Any]) -> str:
    """One screen-reader line for a folio: ``<type> <title>  <short-id>``."""
    ftype = folio.get("type") or "folio"
    title = _title_line(folio.get("title")) or _title_line(folio.get("content")) or "(untitled)"
    return f"{ftype} {title}  {_short(folio['content_hash'])}"


def _emit_json(payload: Any) -> None:
    click.echo(_json.dumps(payload, indent=2, ensure_ascii=False, default=str))


# --- import fidelity gate ---------------------------------------------------
#
# The gate is the only risk surface of an otherwise purely-additive cutover, so
# a false "FIDELITY OK" is how data loss slips into a real tome cutover. It must
# not trust the importer's self-tally; it RECONCILES the report against the
# destination store it actually wrote (fell-r1 findings 1-3).
#
# Hard invariants (any violation => FIDELITY FAILED, exit non-zero):
#
#   Store reconciliation (authoritative — these reconcile counts against the
#   destination store, so they catch loss the self-tally cannot):
#     1. count_folios()  == folios_carried + sites_carried
#        Every legacy folio and every site both go through store.create_folio
#        (a site is a real type=site folio). create_folio is INSERT OR IGNORE on
#        the content hash, so any two inputs that hash identically yield ONE row:
#        a collision (folio/folio, site/site, or folio/site — only the first of
#        which folio_hash_collisions counts) makes the store smaller than the
#        carried tally and trips this. In a faithful import the store holds
#        exactly one row per carried folio plus one per carried site.
#     2. count_threads() == threads_carried + within_threads
#        threads_carried is the count of DISTINCT thread hashes the thread loop
#        wrote (== len(legacy_per_hash); the merged duplicates already collapsed
#        into it). within_threads is one synthesized membership edge per member
#        folio. Legacy stores hold no type=within edges, so the two sets are
#        disjoint and the store holds their sum. This bound is what makes thread
#        loss catchable at all (threads_carried + threads_merged == threads_seen
#        is a structural identity that cannot fail — finding 3); a dropped or
#        unexpectedly-collapsed thread shows up here as a store shortfall.
#     3. count_aliases() == folios_carried
#        One alias per legacy folio_id (a legacy PK, hence distinct). Confirms the
#        alias writes actually landed for every carried folio.
#
#   Documented tripwires (must stay 0 by construction; surfaced loudly if not):
#     4. folio_hash_collisions == 0   (distinct legacy folios that hashed alike)
#     5. actor_endpoints_dropped == 0 (an actor identity with no home — the design
#        guarantees this never happens; the tripwire proves it)
#     6. NOT (folios_with_site_id > 0 AND sites_seen == 0) — folios reference sites
#        but none were imported (finding 1): an empty/missing sites_dir would
#        otherwise drop every site and every membership edge while sites_seen==
#        sites_carried==0 passed the lockstep check silently.
#
#   Internal-consistency checks (cheap self-tally; the reconciliations above are
#   the real teeth, but these catch a future bridge change that breaks lockstep):
#     7. folios_carried == folios_seen
#     8. sites_carried  == sites_seen
#
# Everything else the report counts — threads merged, actor endpoints folded/kept,
# unresolved refs, within_unresolved, status-without-thread, dropped legacy
# columns (the accepted pure-threads + v0 multi-actor-thread drops,
# finding-20260529-y2d6) — is EXPECTED, not a failure. Those are non-gated losses:
# surfaced in the verdict (see _surfaced_losses) so "FIDELITY OK" never overclaims
# "nothing lost" when it means "nothing lost that the hard invariants cover".


class _StoreCounts(NamedTuple):
    """Destination-store row counts gathered right after an import, for the gate
    to reconcile the report against what was actually written."""

    folios: int
    threads: int
    aliases: int


def _fidelity_failures(
    report: ImportReport, counts: _StoreCounts
) -> List[Tuple[str, str]]:
    """Return failed hard invariants as ``(invariant, offending counts)`` pairs."""
    failures: List[Tuple[str, str]] = []

    # Store reconciliation — the authoritative checks (findings 1-3).
    expected_folios = report.folios_carried + report.sites_carried
    if counts.folios != expected_folios:
        failures.append((
            "count_folios() == folios_carried + sites_carried",
            f"store has {counts.folios}, expected {expected_folios} "
            f"({report.folios_carried} folios + {report.sites_carried} sites)",
        ))
    expected_threads = report.threads_carried + report.within_threads
    if counts.threads != expected_threads:
        failures.append((
            "count_threads() == threads_carried + within_threads",
            f"store has {counts.threads}, expected {expected_threads} "
            f"({report.threads_carried} carried + {report.within_threads} within)",
        ))
    if counts.aliases != report.folios_carried:
        failures.append((
            "count_aliases() == folios_carried",
            f"store has {counts.aliases}, expected {report.folios_carried}",
        ))

    # Documented tripwires.
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
    if report.folios_with_site_id and report.sites_seen == 0:
        failures.append((
            "sites imported when folios reference them",
            f"{report.folios_with_site_id} folios carry a site_id but 0 sites "
            "were seen (empty or missing sites dir)",
        ))

    # Internal-consistency self-tally.
    if report.folios_carried != report.folios_seen:
        failures.append((
            "folios_carried == folios_seen",
            f"carried {report.folios_carried} of {report.folios_seen} seen",
        ))
    if report.sites_carried != report.sites_seen:
        failures.append((
            "sites_carried == sites_seen",
            f"carried {report.sites_carried} of {report.sites_seen} seen",
        ))
    return failures


def _surfaced_losses(report: ImportReport) -> List[str]:
    """Non-gated loss counters that are nonzero, as ``name=value`` strings.

    These are real un-migrated data (some unmigratable by design): they do not
    fail the gate, but a bare "FIDELITY OK" must not be printed while any is
    nonzero — it would read as "nothing lost" when the truth is "nothing lost
    that the hard invariants cover" (finding 4)."""
    parts: List[str] = []
    if report.within_unresolved:
        parts.append(f"within_unresolved={report.within_unresolved}")
    if report.status_without_thread:
        parts.append(f"status_without_thread={report.status_without_thread}")
    if report.threads_merged:
        parts.append(f"threads_merged={report.threads_merged}")
    if report.sites_skipped_no_id:
        parts.append(f"sites_skipped_no_id={report.sites_skipped_no_id}")
    if report.dropped_folio_columns:
        parts.append(
            "dropped_folio_columns="
            + ",".join(sorted(report.dropped_folio_columns))
        )
    if report.dropped_thread_columns:
        parts.append(
            "dropped_thread_columns="
            + ",".join(sorted(report.dropped_thread_columns))
        )
    return parts


def _emit_fidelity(report: ImportReport, counts: _StoreCounts) -> bool:
    """Print the fidelity verdict. Return True when every hard invariant holds.

    A clean gate prints a bare "FIDELITY OK" only when no non-gated loss counter
    is set; otherwise it prints a qualified "FIDELITY OK (...)" that enumerates
    the surfaced losses, so the one-line verdict distinguishes "nothing lost" from
    "nothing lost that the hard invariants cover". Either way the gate passes
    (exit 0); only a hard-invariant failure prints "FIDELITY FAILED"."""
    failures = _fidelity_failures(report, counts)
    if failures:
        click.echo("FIDELITY FAILED")
        for invariant, detail in failures:
            click.echo(f"  invariant {invariant} violated: {detail}")
        return False
    surfaced = _surfaced_losses(report)
    if surfaced:
        click.echo(
            "FIDELITY OK (gated invariants pass; surfaced non-gated losses: "
            + "; ".join(surfaced)
            + ")"
        )
    else:
        click.echo("FIDELITY OK")
    return True


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
) -> Tuple[ImportReport, _StoreCounts]:
    """Resolve paths, open the target station, and run the read-only import.

    Returns the report together with the destination-store counts (gathered
    while the station is open) so the fidelity gate can reconcile against them.
    """
    db_path, sd_path = _resolve_legacy_paths(project_root, legacy_db, sites_dir)
    if db_path is None:
        raise click.ClickException(
            "give a PROJECT_ROOT, or both --legacy-db and --sites-dir."
        )
    if not Path(db_path).is_file():
        raise click.ClickException(f"no legacy database at {db_path}")
    # A sites dir DERIVED from a project root must exist, mirroring the db check
    # (finding 1). A missing/empty sites dir would drop every site and every
    # membership edge while the gate still passed; fail fast instead. An explicit
    # --sites-dir override is the caller's deliberate choice and is not policed
    # here (the finding-1 gate check still catches the empty case at verify time).
    sites_derived = sites_dir is None and project_root is not None
    if sites_derived and not Path(sd_path).is_dir():
        raise click.ClickException(
            f"no legacy sites dir at {sd_path} "
            "(expected PROJECT_ROOT/.skein/data/sites; pass --sites-dir to override)"
        )
    with _open_station(ctx) as station:
        report = import_project(db_path, sd_path, station.store)
        counts = _StoreCounts(
            folios=station.store.count_folios(),
            threads=station.store.count_threads(),
            aliases=station.store.count_aliases(),
        )
    return report, counts


@click.group()
@click.option(
    "--data-dir",
    default=None,
    help=f"Station data dir (default: ${ENV_DATA_DIR} or ./.skein-next).",
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


# --- publish (client -> instance) -------------------------------------------


@cli.command()
@click.argument("refs", nargs=-1)
@click.option("--site", "site_slug", default=None, help="Publish every folio in this site.")
@click.option("--to", "instance_url", default=None, help="Instance publish URL (e.g. http://127.0.0.1:9101).")
@click.option("--by", "created_by", default=None, help="Author recording the publish (default: $SKEIN_NEXT_AGENT).")
@click.option("--dry-run", is_flag=True, help="Show what would be published; send nothing.")
@click.option("--sign", "do_sign", is_flag=True, help="Sign each folio at the boundary (needs --login or --oidc-token).")
@click.option("--login", is_flag=True, help="With --sign: run the interactive Sigstore login here.")
@click.option("--oob", "force_oob", is_flag=True, help="With --login: out-of-band code flow (no local browser, e.g. SSH).")
@click.option("--oidc-issuer", default="https://accounts.google.com", help="OIDC issuer for --sign --oidc-token.")
@click.option("--oidc-token", default=None, help="OIDC JWT for --sign ('-' reads stdin).")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def publish(
    ctx: click.Context,
    refs: Tuple[str, ...],
    site_slug: Optional[str],
    instance_url: Optional[str],
    created_by: Optional[str],
    dry_run: bool,
    do_sign: bool,
    login: bool,
    force_oob: bool,
    oidc_issuer: str,
    oidc_token: Optional[str],
    output_json: bool,
) -> None:
    """Publish local folios to an instance.

    Give folio REFS, a --site SLUG, or both. Each folio travels with its site
    folio and its in-set threads (membership, status); the instance verifies
    every content hash before storing. Without --sign the batch crosses unsigned
    (pre-mesh). With --sign each folio is signed at the boundary under your OIDC
    identity (the human-accountability gate) and the provenance card flips from
    UNSIGNED to your authorship.

    Examples:
      interskein publish --site specifications --to http://127.0.0.1:9101
      interskein publish --site specifications --to https://interskein.com --sign --login
    """
    if not refs and not site_slug:
        raise click.ClickException("nothing to publish — give folio REFS or --site SLUG.")
    if not dry_run and not instance_url:
        raise click.ClickException("--to INSTANCE_URL is required (or use --dry-run).")
    if (login or force_oob or oidc_token) and not do_sign:
        raise click.ClickException("--login/--oob/--oidc-token only apply with --sign.")
    if login and oidc_token:
        raise click.ClickException("use one of --login or --oidc-token, not both.")
    signer = (
        _build_signer(oidc_issuer, oidc_token, login=login, force_oob=force_oob)
        if do_sign
        else None
    )
    author = created_by or _default_author()
    with _open_station(ctx) as station:
        try:
            result = _publish.publish(
                station,
                instance_url or "(dry-run)",
                refs=list(refs) or None,
                site=site_slug,
                by=author,
                dry_run=dry_run,
                signer=signer,
            )
        except (UnknownSite, UnknownFolio) as e:
            raise click.ClickException(str(e))
        except PublishError as e:
            raise click.ClickException(str(e))

    if output_json:
        _emit_json(result)
        return
    if not result.get("folios"):
        click.echo("nothing to publish (selection is empty)")
        return
    verb = "would publish" if dry_run else "published"
    signed_note = " (signed)" if result.get("signed") else ""
    click.echo(f"{verb} {result['folios']} folio(s), {result['threads']} thread(s){signed_note}")
    if dry_run:
        for h in result.get("folio_hashes", []):
            click.echo(_short(h, 24))
        return
    ack = result.get("ack", {})
    click.echo(
        f"instance accepted {len(ack.get('accepted', []))} new, "
        f"{len(ack.get('existing', []))} already held, "
        f"{len(ack.get('rejected', []))} rejected"
    )
    for r in ack.get("rejected", []):
        click.echo(f"rejected {_short(r.get('content_hash') or '?', 24)} - {r.get('reason')}")
    rejected_threads = result.get("threads_rejected", [])
    if rejected_threads:
        click.echo(f"{len(rejected_threads)} link(s) not carried:")
        for r in rejected_threads:
            click.echo(f"  {_short(r.get('thread_hash') or '?', 24)} - {r.get('reason')}")


@cli.command()
def login() -> None:
    """Run the interactive Sigstore login; print the OIDC token to stdout.

    The human-accountability gate: opens a browser. The token is short-lived —
    use it promptly. Diagnostics go to stderr so the token captures cleanly:

      TOKEN=$(interskein login) && interskein publish --site S --to URL --sign --oidc-token "$TOKEN"

    On a headless/SSH box (no local browser, can't capture), use the inline
    out-of-band flow instead — its code prompt stays on the terminal:

      interskein publish --site S --to URL --sign --login --oob

    Or, with a browser, skip the token juggling: interskein publish ... --sign --login
    """
    from . import sign as _sign

    provider = _sign.acquire_oidc_provider()
    click.echo(f"signed in as {provider.issuer}", err=True)
    click.echo(provider.token)


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind host.")
@click.option("--port", default=9101, type=int, help="Bind port (default 9101).")
@click.pass_context
def ingress(ctx: click.Context, host: str, port: int) -> None:
    """Serve the publish ingress write surface (PROTOTYPE, unsigned).

    Receives publish batches from clients and stores them into this station's
    data dir (--data-dir or $SKEIN_NEXT_DATA_DIR), which the read web surface
    then serves. Distinct port from the read app so the read surface stays
    read-only.
    """
    data_dir = ctx.obj.get("data_dir")
    if data_dir:
        os.environ[ENV_DATA_DIR] = data_dir
    from .ingress import run_server  # lazy: keep web deps off the other verbs

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
    report, counts = _run_import(ctx, project_root, legacy_db, sites_dir)
    click.echo(report.render())
    if verify:
        if not _emit_fidelity(report, counts):
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

    Read-only on the source; re-writes the idempotent target. Re-runs the import
    to recompute the report and re-derive the destination store, prints the
    report, then reconciles the store against the report's hard invariants. Exits
    non-zero if any fails.
    """
    report, counts = _run_import(ctx, project_root, legacy_db, sites_dir)
    click.echo(report.render())
    if not _emit_fidelity(report, counts):
        ctx.exit(1)


# --- account bindings (operator / author authorization) ---------------------


@cli.group()
def account() -> None:
    """Manage the instance's account bindings (operator + authors).

    The authorization sidecar: who may write to this instance under require_signed.
    Identity is the verified Sigstore (issuer, subject) pair. There is exactly one
    active operator; authors are vouched for by the operator."""


@account.command("init-operator")
@click.option("--issuer", required=True, help="The operator's OIDC issuer.")
@click.option("--subject", required=True, help="The operator's OIDC subject.")
@click.pass_context
def account_init_operator(ctx: click.Context, issuer: str, subject: str) -> None:
    """Bootstrap the self-vouched root operator (refuses a second active one)."""
    from .authorization import Principal, OperatorAlreadyBootstrapped, bootstrap_operator

    with _open_station(ctx) as st:
        try:
            bootstrap_operator(st.store, Principal(issuer, subject))
        except OperatorAlreadyBootstrapped as e:
            raise click.ClickException(f"OperatorAlreadyBootstrapped: {e}")
    click.echo(f"operator {issuer}/{subject}")


@account.command("add")
@click.option("--issuer", required=True)
@click.option("--subject", required=True)
@click.option("--role", default="author", type=click.Choice(["author"]))
@click.pass_context
def account_add(ctx: click.Context, issuer: str, subject: str, role: str) -> None:
    """Add an author binding, vouched for by the active operator."""
    with _open_station(ctx) as st:
        op = st.store.get_operator()
        if op is None:
            raise click.ClickException("no operator; run init-operator first")
        st.store.add_binding(
            issuer, subject, role=role,
            vouched_by_issuer=op.issuer, vouched_by_subject=op.subject,
        )
    click.echo(f"{role} {issuer}/{subject}")


@account.command("revoke")
@click.option("--issuer", required=True)
@click.option("--subject", required=True)
@click.pass_context
def account_revoke(ctx: click.Context, issuer: str, subject: str) -> None:
    """Revoke a binding (revocation is not deletion). Refuses the active operator."""
    with _open_station(ctx) as st:
        op = st.store.get_operator()
        if op is not None and op.issuer == issuer and op.subject == subject:
            raise click.ClickException(
                "refusing to revoke the active operator; use account rotate-operator "
                "to hand off, or init-operator after a deliberate teardown"
            )
        if not st.store.revoke_binding(issuer, subject):
            raise click.ClickException(f"no active binding for {issuer}/{subject}")
    click.echo(f"revoked {issuer}/{subject}")


@account.command("rotate-operator")
@click.option("--new-issuer", required=True)
@click.option("--new-subject", required=True)
@click.pass_context
def account_rotate_operator(ctx: click.Context, new_issuer: str, new_subject: str) -> None:
    """Hand off the operator role atomically (revoke old + install new in one tx)."""
    with _open_station(ctx) as st:
        old = st.store.get_operator()
        if old is None:
            raise click.ClickException("no operator to rotate; run init-operator")
        with st.store.transaction():
            st.store.revoke_binding(old.issuer, old.subject, event="rotated_out")
            if st.store.get_binding(new_issuer, new_subject) is not None:
                st.store.promote_to_operator(new_issuer, new_subject)  # preserves created_at
            else:
                st.store.add_binding(
                    new_issuer, new_subject, role="operator",
                    vouched_by_issuer=old.issuer, vouched_by_subject=old.subject,
                    event="rotated_in",
                )
    click.echo(f"operator {new_issuer}/{new_subject}")


@account.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include revoked bindings.")
@click.pass_context
def account_list(ctx: click.Context, show_all: bool) -> None:
    """List bindings, one per line: ``<role> <issuer>/<subject>`` (a11y plain text)."""
    with _open_station(ctx) as st:
        for b in st.store.list_bindings(include_revoked=show_all):
            suffix = " (revoked)" if b.revoked_at else ""
            click.echo(f"{b.role} {b.issuer}/{b.subject}{suffix}")


@cli.group()
def maintenance() -> None:
    """Instance maintenance verbs (run when the ingress is quiesced)."""


@maintenance.command("verify-cache")
@click.pass_context
def maintenance_verify_cache(ctx: click.Context) -> None:
    """Backfill the manifest signature-verdict cache over the stored corpus."""
    from .ingress import backfill_verify_cache

    with _open_station(ctx) as st:
        n = backfill_verify_cache(st)
    click.echo(f"backfilled {n} manifest verdict(s)")


def main() -> None:
    """Entry point for the ``interskein`` console script."""
    cli(prog_name="interskein")


if __name__ == "__main__":
    main()
