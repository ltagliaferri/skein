"""The ``skein station`` command group — launchers + station operations.

Station re-home Stage 6 (design §3, §5 Stage 6, §10 #6). Re-homed from
``skein_next/cli.py``'s server launchers (``serve``/``ingress``/``maintenance``)
and station-ops verbs (``account``/``invite``/``redeem-invite``/``whoami``/
``login``); the fat-client *authoring* verbs are DROP (the working skein authors
over its 8001 API).

Unlike the rest of the ``skein`` CLI — a thin client over the 8001 workbench API
— this group operates on a station corpus **data dir directly**, or launches the
two station servers over it. That is deliberate, not a porting shortcut: under
``require_signed`` the ingress refuses to boot until exactly one active operator
exists, so ``account init-operator`` necessarily runs *before* any server is up,
and binding management stays off the network surface (the trust anchor is shell
access to the box). The client-facing verbs (``redeem-invite``, ``login``,
``whoami``) do speak HTTP / run the Sigstore ceremony.

The data dir comes from ``--data-dir``, else ``$SKEIN_STATION_DATA_DIR``, else
``./.skein-station``.

Output is plain text built for a screen reader: one item per line, a human
description followed by the id used to look it up — no tables, no columns, no
markdown. ``--json`` on read verbs emits structured data for tooling.
"""

from __future__ import annotations

import json as _json
import os
import secrets
import shlex
from typing import Any, Dict, Optional

import click

from .station_env import ENV_DATA_DIR, StationEnvError, station_env

# The bare-default corpus location. A DELIBERATE rename from skein_next's
# ``./.skein-next`` default — the fifth named delta of the Stage-6 re-home,
# approved with the env-key rename (Patrick, 2026-07-10): both live containers
# always set the data-dir env explicitly, so this only affects bare local use.
DEFAULT_DATA_DIR = ".skein-station"


def _emit_json(payload: Dict[str, Any]) -> None:
    click.echo(_json.dumps(payload, indent=2, sort_keys=True))


def _data_dir(ctx: click.Context) -> str:
    explicit = ctx.obj.get("station_data_dir")
    if explicit:
        return explicit
    try:
        return station_env("DATA_DIR") or DEFAULT_DATA_DIR
    except StationEnvError as e:
        raise click.ClickException(str(e))


def _open_station(ctx: click.Context):
    from .station import Station  # lazy: keep sqlite/store off --help paths

    return Station(_data_dir(ctx))


def _export_data_dir(ctx: click.Context) -> None:
    """Hand the RESOLVED data dir to the server process env, unambiguously.

    ``_data_dir`` already applied the precedence (--data-dir wins, else env,
    else default), so write the canonical key with that resolved value."""
    os.environ[ENV_DATA_DIR] = _data_dir(ctx)


@click.group()
@click.option(
    "--data-dir",
    "data_dir",
    default=None,
    help=f"Station data dir (default: $SKEIN_STATION_DATA_DIR or ./{DEFAULT_DATA_DIR}).",
)
@click.pass_context
def station(ctx: click.Context, data_dir: Optional[str]) -> None:
    """Run and operate a public station (read + ingress servers, operator ops).

    A station is this codebase in its public role: the ingress write surface
    (:9101) receiving signed publishes, and the read-only web surface (:9001)
    serving them — selected by config + data dir, over one corpus. The verbs
    here launch those servers and run the operator-side account/invite
    ceremonies a signed station needs to boot and onboard collaborators.
    """
    ctx.ensure_object(dict)
    ctx.obj["station_data_dir"] = data_dir


# --- launchers (the two live launch points repoint here at Stage 7b) ---------


@station.command()
@click.option("--host", default="127.0.0.1", help="Bind host.")
@click.option("--port", default=9001, type=int, help="Bind port (default 9001).")
@click.pass_context
def serve(ctx: click.Context, host: str, port: int) -> None:
    """Serve the read-only web surface (default http://127.0.0.1:9001).

    The web app reads the station data dir via --data-dir or
    $SKEIN_STATION_DATA_DIR. FastAPI/uvicorn are imported only when this runs.
    """
    _export_data_dir(ctx)
    from .station import StationBootError  # lazy: keeps sqlite/store off --help paths
    from .web.app import run_server  # lazy: keep the heavy web deps off other verbs

    try:
        run_server(host=host, port=port)
    except (StationEnvError, StationBootError) as e:
        raise click.ClickException(str(e))


@station.command()
@click.option("--host", default="127.0.0.1", help="Bind host.")
@click.option("--port", default=9101, type=int, help="Bind port (default 9101).")
@click.pass_context
def ingress(ctx: click.Context, host: str, port: int) -> None:
    """Serve the publish ingress write surface (default http://127.0.0.1:9101).

    Receives signed publish batches and invite redemptions and stores them into
    this station's data dir, which the read web surface then serves. Distinct
    port from the read app so the read surface stays read-only. Under
    $SKEIN_STATION_REQUIRE_SIGNED=1 boot refuses unless exactly one active
    operator exists (run ``skein station account init-operator`` first).
    """
    _export_data_dir(ctx)
    from . import ingress as _ingress  # lazy: keep web deps off the other verbs
    from .station import StationBootError

    try:
        _ingress.run_server(host=host, port=port)
    except (
        StationEnvError,
        _ingress.RequireSignedConfigError,
        _ingress.OperatorInvariantError,
        StationBootError,
    ) as e:
        raise click.ClickException(str(e))


@station.group()
def maintenance() -> None:
    """Station maintenance verbs (run when the ingress is quiesced)."""


@maintenance.command("verify-cache")
@click.pass_context
def maintenance_verify_cache(ctx: click.Context) -> None:
    """Backfill the manifest signature-verdict cache over the stored corpus."""
    from .ingress import backfill_verify_cache

    with _open_station(ctx) as st:
        n = backfill_verify_cache(st)
    click.echo(f"backfilled {n} manifest verdict(s)")


# --- account bindings (operator / author authorization) ---------------------


@station.group()
def account() -> None:
    """Manage the station's account bindings (operator + authors).

    The authorization sidecar: who may write to this station under
    require_signed. Identity is the verified Sigstore (issuer, subject) pair.
    There is exactly one active operator; authors are vouched for by the
    operator."""


@account.command("init-operator")
@click.option("--issuer", required=True, help="The operator's OIDC issuer.")
@click.option("--subject", required=True, help="The operator's OIDC subject.")
@click.pass_context
def account_init_operator(ctx: click.Context, issuer: str, subject: str) -> None:
    """Bootstrap the self-vouched root operator (refuses a second active one)."""
    from .authorization import OperatorAlreadyBootstrapped, Principal, bootstrap_operator

    with _open_station(ctx) as st:
        try:
            bootstrap_operator(st.store, Principal(issuer, subject))
        except OperatorAlreadyBootstrapped as e:
            raise click.ClickException(f"OperatorAlreadyBootstrapped: {e}")
    click.echo(f"operator {issuer}/{subject}")


@account.command("add")
@click.option("--issuer", required=True)
@click.option("--subject", required=True)
@click.option(
    "--role",
    default="originator",
    type=click.Choice(["originator", "administrator", "steward"]),
    help="Tier to bind (default originator). administrator is the privileged LOCAL-only "
    "bind (never wire-redeemable); operator is init-operator/rotate-operator only.",
)
@click.pass_context
def account_add(ctx: click.Context, issuer: str, subject: str, role: str) -> None:
    """Add a binding at a tier (originator/administrator/steward), vouched for by the
    active operator. administrator is a privileged LOCAL-only bind (never wire-redeemable);
    originator and steward may ALSO be onboarded over the wire via invites; operator is
    installed only via init-operator/rotate-operator."""
    with _open_station(ctx) as st:
        op = st.store.get_operator()
        if op is None:
            raise click.ClickException("no operator; run init-operator first")
        b = st.store.add_binding(
            issuer,
            subject,
            role=role,
            vouched_by_issuer=op.issuer,
            vouched_by_subject=op.subject,
        )
    # Echo the binding's ACTUAL stored role, not the requested one. Adding
    # --role author onto the active operator hits add_binding's already-active
    # idempotent no-op (the binding correctly STAYS operator); printing the
    # requested "author" would misreport the stored state.
    click.echo(f"{b.role} {issuer}/{subject}")


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
        # Read the current operator INSIDE the write transaction (deep_code_audit,
        # fell r4): two rotations racing on the same corpus both read the same
        # pre-lock operator, and the loser's unchecked revoke (0 rows) would let
        # it install a SECOND active operator — the exact invariant the ingress
        # boot check exists to enforce. Inside the lock the read is current, and
        # a False revoke means the operator changed underneath us: refuse.
        with st.store.transaction():
            old = st.store.get_operator()
            if old is None:
                raise click.ClickException("no operator to rotate; run init-operator")
            if (new_issuer, new_subject) == (old.issuer, old.subject):
                raise click.ClickException(
                    "refusing to rotate the operator onto itself "
                    f"({new_issuer}/{new_subject} is already the active operator)"
                )
            if not st.store.revoke_binding(old.issuer, old.subject, event="rotated_out"):
                raise click.ClickException(
                    "rotation raced: the active operator changed underneath this "
                    "command; check 'skein station account list' and re-run"
                )
            if st.store.get_binding(new_issuer, new_subject) is not None:
                st.store.promote_to_operator(new_issuer, new_subject)  # preserves created_at
            else:
                st.store.add_binding(
                    new_issuer,
                    new_subject,
                    role="operator",
                    vouched_by_issuer=old.issuer,
                    vouched_by_subject=old.subject,
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


@account.command("rebind")
@click.option("--issuer", required=True)
@click.option("--subject", required=True)
@click.option(
    "--role", required=True,
    type=click.Choice(["originator", "administrator", "steward"]),
    help="New tier for an ACTIVE binding (operator changes go through rotate-operator).",
)
@click.pass_context
def account_rebind(ctx: click.Context, issuer: str, subject: str, role: str) -> None:
    """Change an active binding's tier locally (a privileged rebind). Refuses to touch
    the operator role in either direction — operator handoff is rotate-operator."""
    with _open_station(ctx) as st:
        try:
            b = st.store.set_role(issuer, subject, role)
        except ValueError as e:
            raise click.ClickException(str(e))
        if b is None:
            raise click.ClickException(f"no active binding for {issuer}/{subject}")
    click.echo(f"{b.role} {issuer}/{subject}")


# --- grant (per-document delegation of a to-end right) ------------------------

_GRANT_KINDS = ["supersede", "site_contribute", "site_edit"]


@station.group()
def grant() -> None:
    """Issue, revoke, and list per-document grants — a delegated TO-end right on ONE
    lineage (anchor = its genesis hash). Issued by the operator (administrator/operator
    only, §3.2). A grant never satisfies the pure per-folio from-end."""


@grant.command("issue")
@click.option("--anchor", required=True, help="Lineage GENESIS content hash.")
@click.option("--issuer", required=True, help="Grantee issuer.")
@click.option("--subject", required=True, help="Grantee subject.")
@click.option("--kind", required=True, type=click.Choice(_GRANT_KINDS))
@click.pass_context
def grant_issue(ctx: click.Context, anchor: str, issuer: str, subject: str, kind: str) -> None:
    """Issue a grant, vouched for by the active operator."""
    with _open_station(ctx) as st:
        op = st.store.get_operator()
        if op is None:
            raise click.ClickException("no operator; run init-operator first")
        st.store.add_grant(anchor, issuer, subject, kind, op.issuer, op.subject)
    click.echo(f"granted {kind} on {anchor} to {issuer}/{subject}")


@grant.command("revoke")
@click.option("--anchor", required=True)
@click.option("--issuer", required=True)
@click.option("--subject", required=True)
@click.option("--kind", required=True, type=click.Choice(_GRANT_KINDS))
@click.pass_context
def grant_revoke(ctx: click.Context, anchor: str, issuer: str, subject: str, kind: str) -> None:
    """Revoke ONE grant (revocation is not deletion)."""
    with _open_station(ctx) as st:
        if not st.store.revoke_grant(anchor, issuer, subject, kind):
            raise click.ClickException(
                f"no active {kind} grant on {anchor} for {issuer}/{subject}"
            )
    click.echo(f"revoked {kind} on {anchor} for {issuer}/{subject}")


@grant.command("revoke-all-by")
@click.option("--issuer", required=True, help="Granter (vouched-by) issuer.")
@click.option("--subject", required=True, help="Granter (vouched-by) subject.")
@click.pass_context
def grant_revoke_all_by(ctx: click.Context, issuer: str, subject: str) -> None:
    """Containment verb: revoke EVERY active grant vouched by a granter (grants do NOT
    auto-cascade on granter revocation, §3.2). Run this after revoking a compromised
    administrator to contain the grants they issued."""
    with _open_station(ctx) as st:
        n = st.store.revoke_grants_vouched_by(issuer, subject)
    click.echo(f"revoked {n} grant(s) vouched by {issuer}/{subject}")


@grant.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include revoked grants.")
@click.pass_context
def grant_list(ctx: click.Context, show_all: bool) -> None:
    """List grants, one per line (a11y plain text):
    ``<kind> <anchor> <grantee_issuer>/<grantee_subject>``."""
    with _open_station(ctx) as st:
        for g in st.store.list_grants(include_revoked=show_all):
            suffix = " (revoked)" if g.get("revoked_at") else ""
            click.echo(
                f"{g['kind']} {g['anchor_hash']} "
                f"{g['grantee_issuer']}/{g['grantee_subject']}{suffix}"
            )


@station.command("slug-override")
@click.option("--slug", required=True)
@click.option(
    "--anchor", required=True,
    help="A held folio in the target lineage; the slug is anchored at that lineage's "
    "GENESIS (resolve_slug then derives the head).",
)
@click.pass_context
def slug_override(ctx: click.Context, slug: str, anchor: str) -> None:
    """Re-point a slug to a lineage as the operator (a privileged override of a claim
    held by another signer, §6). The slug's claimant becomes the operator, and the
    anchor is normalized to the lineage GENESIS so members filed under the genesis keep
    their breadcrumb (a head-anchored slug would orphan them)."""
    from .thread_authz import LineageReject, lineage_genesis_for

    with _open_station(ctx) as st:
        op = st.store.get_operator()
        if op is None:
            raise click.ClickException("no operator; run init-operator first")
        # Refuse an anchor that names no held lineage — the slug would resolve to None
        # (resolve_slug finds no head), so the "override" would silently break the name.
        if st.store.get_folio(anchor) is None:
            raise click.ClickException(
                f"anchor {anchor} is not a held folio on this station; "
                "the slug would resolve to nothing"
            )
        try:
            genesis = lineage_genesis_for(st.store, anchor).hash
        except LineageReject as e:
            # Name the recovery, not just the dead end. This is an operator-local
            # surface, and the public entry is the migration MODULE, not the
            # `_repair_supersedes` helper the earlier brief named — that helper takes an
            # open connection with no transaction, so running it directly is an unguarded
            # partial migration. `migrate()` (which the module entry calls) wraps the same
            # repair in one BEGIN IMMEDIATE and is idempotent. Interpolate the station's
            # own db path so the command is copy-pasteable, not a placeholder to guess.
            from .station_store import DB_FILENAME

            db_path = os.path.abspath(os.path.join(_data_dir(ctx), DB_FILENAME))
            raise click.ClickException(
                f"anchor {anchor} has no single lineage genesis ({e}); "
                "repair the lineage before naming it: stop the station and run "
                f"`python -m skein.migrations.perm_model_rev6 {shlex.quote(db_path)}`, "
                "which quarantines the offending supersedes rows atomically"
            )
        status = st.store.claim_slug(slug, genesis, op.issuer, op.subject, override=True)
    click.echo(f"slug {slug} -> {genesis} ({status})")


# --- invite (one-time collaborator onboarding tokens) -------------------------

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(s: str):
    """Parse a ``30m`` / ``24h`` / ``7d`` / ``3600s`` duration to a timedelta."""
    import re
    from datetime import timedelta

    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", s or "")
    if not m:
        raise click.ClickException("--expires must look like 30m, 24h, 7d, or 3600s")
    return timedelta(seconds=int(m.group(1)) * _DURATION_UNITS[m.group(2)])


def _invite_state(row: Dict[str, Any], now) -> str:
    """Derive the display state of an invite row (revoked > redeemed > expired > outstanding)."""
    from datetime import datetime, timezone

    if row.get("revoked_at"):
        return "revoked"
    if row.get("used_at"):
        return "redeemed"
    exp = row.get("expires_at")
    if exp:
        try:
            dt = datetime.fromisoformat(exp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt <= now:
                return "expired"
        except ValueError:
            pass
    return "outstanding"


def _invite_line(row: Dict[str, Any], state: str) -> str:
    """One screen-reader line for an invite (plain text: ``<description> <id>``)."""
    note = f' "{row["note"]}"' if row.get("note") else ""
    short = (row.get("token_hash") or "?")[:12]
    role = row.get("role") or "originator"
    if state == "redeemed":
        return f"redeemed {role} by {row.get('bound_subject')} at {row.get('redeemed_at')}{note} {short}"
    if state == "outstanding":
        return f"outstanding {role} expires {row.get('expires_at')}{note} {short}"
    return f"{state} {role}{note} {short}"


def _resolve_invite_hash(st, prefix: str) -> str:
    """Resolve a token-hash prefix to exactly one invite's full hash, or error."""
    # An empty prefix startswith-matches EVERY row — with exactly one invite in
    # the corpus, a script's failed variable interpolation (--hash "") would
    # silently revoke it (deep_code_audit, fell r4). Reject it like any other
    # non-matching input.
    if not (prefix or "").strip():
        raise click.ClickException("empty invite hash prefix")
    matches = [
        r["token_hash"]
        for r in st.store.list_invites(include_inactive=True)
        if r["token_hash"] == prefix or r["token_hash"].startswith(prefix)
    ]
    matches = sorted(set(matches))
    if not matches:
        raise click.ClickException(f"no invite matching hash {prefix!r}")
    if len(matches) > 1:
        raise click.ClickException(
            f"ambiguous hash prefix {prefix!r} matches {len(matches)} invites"
        )
    return matches[0]


@station.group()
def invite() -> None:
    """Mint, list, and revoke one-time collaborator invites.

    An invite is a bearer token the operator sends out of band (vouching for a
    human). The collaborator's agent redeems it via ``skein station
    redeem-invite``, which runs a token-bound Sigstore ceremony and auto-binds
    the discovered identity as an author. The plaintext token is shown ONCE at
    mint and never stored — only its hash."""


def _mint_token() -> str:
    """A 256-bit URL-safe bearer token that never starts with ``-``.

    token_urlsafe's alphabet includes ``-``, and a token beginning with one
    reads as an option on the positional revoke/redeem verbs, making ~1.6% of
    invites fail with an opaque "No such option" (issue-20260723-rzer).
    Rejection sampling keeps the accepted set uniform. Tokens minted before
    this guard can still start with ``-``; the verbs accept those after a
    ``--`` separator.
    """
    while True:
        token = secrets.token_urlsafe(32)  # 32 bytes = 256-bit CSPRNG token
        if not token.startswith("-"):
            return token


@invite.command("mint")
@click.option(
    "--role",
    default="originator",
    type=click.Choice(["originator", "steward"]),
    help="Wire-redeemable tier for this invite (default originator). Only "
    "originator/steward may be minted — operator/administrator are never wire-bound.",
)
@click.option("--expires", default="7d", help="Validity window (e.g. 30m, 24h, 7d). Default 7d.")
@click.option("--note", default=None, help="Operator note (who this invite is for).")
@click.option(
    "--origin",
    default=None,
    help="Publish-ingress origin used by redeem (default: $SKEIN_STATION_ORIGIN).",
)
@click.option(
    "--onboarding-origin",
    default=None,
    help="Read-surface origin hosting /onboarding (default: $SKEIN_STATION_BASE_URL).",
)
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def invite_mint(
    ctx: click.Context,
    role: str,
    expires: str,
    note: Optional[str],
    origin: Optional[str],
    onboarding_origin: Optional[str],
    output_json: bool,
) -> None:
    """Mint a one-time invite; print the token + a ready-to-send blurb (token shown ONCE)."""
    from datetime import datetime, timezone

    from urllib.parse import urlsplit

    from . import bootstrap_pack
    from .identity import hash_token
    from .publish import canonical_instance

    delta = _parse_duration(expires)
    try:
        origin = origin or station_env("ORIGIN")
        onboarding_origin = onboarding_origin or station_env("BASE_URL")
    except StationEnvError as e:
        raise click.ClickException(str(e))
    def clean_origin(value: Optional[str], label: str) -> Optional[str]:
        if not value:
            return None
        try:
            parts = urlsplit(value.strip())
            if (
                parts.scheme.lower() not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
            ):
                raise ValueError("need an http(s) scheme and host, without credentials/query/fragment")
            return canonical_instance(value)
        except ValueError as e:
            raise click.ClickException(f"invalid {label} {value!r}: {e}")

    origin = clean_origin(origin, "publish origin")
    onboarding_origin = clean_origin(onboarding_origin, "onboarding origin")
    pack_inventory = bootstrap_pack.inventory(_data_dir(ctx))
    pack_ready = bootstrap_pack.is_complete(pack_inventory)
    token = _mint_token()
    token_h = hash_token(token)
    expires_at = datetime.now(timezone.utc) + delta
    with _open_station(ctx) as st:
        op = st.store.get_operator()
        if op is None:
            raise click.ClickException(
                "no operator; run 'skein station account init-operator' first"
            )
        st.store.mint_invite(
            token_h,
            role,
            expires_at,
            vouched_by_issuer=op.issuer,
            vouched_by_subject=op.subject,
            note=note,
        )
    expires_iso = expires_at.isoformat()
    if output_json:
        _emit_json(
            {
                "token": token,
                "token_hash": token_h,
                "role": role,
                "expires_at": expires_iso,
                "note": note,
                "origin": origin,
                "onboarding_origin": onboarding_origin,
                "bootstrap_ready": pack_ready,
                "bootstrap_sha256": {
                    name: pack_inventory[name] for name in bootstrap_pack.RAW_FILES
                },
            }
        )
        return
    origin_display = origin or "<your-station-ingress-origin>"
    onboarding_display = onboarding_origin or "<your-station-read-origin>"
    blurb = (
        f"You're invited to publish on {origin_display}. Hand this entire message to "
        f"your coding agent.\n\n"
        f"One-time invite token (expires {expires_iso}):\n  {token}\n\n"
        "Bootstrap freshness SHA256 (confirm through a Patrick-controlled channel):\n"
        + "".join(
            f"  {name} {pack_inventory[name] or '<unavailable>'}\n"
            for name in bootstrap_pack.RAW_FILES
        )
        + "\n"
        f"Your agent: follow the bootstrap pack at {onboarding_display}/onboarding to install "
        f"the verified station CLI (verify the signed install spec FIRST), then redeem:\n\n"
        f"  skein station redeem-invite {token} --to {origin_display} --login\n\n"
        f"Redeeming runs a Sigstore login and writes a record to the PUBLIC Rekor "
        f"transparency log; on a headless box, confirm before proceeding. After "
        f"redeeming you're bound as an author and can publish signed content."
    )
    click.echo(f"invite token (one-time, expires {expires_iso}):")
    click.echo(f"  {token}")
    click.echo(f"hash {token_h[:12]}")
    if not origin:
        click.echo(
            "warning: no origin (set $SKEIN_STATION_ORIGIN or pass --origin) — the blurb "
            "below has a placeholder; fill it in before sending.",
            err=True,
        )
    if not onboarding_origin:
        click.echo(
            "warning: no onboarding origin (set $SKEIN_STATION_BASE_URL or pass "
            "--onboarding-origin) — the blurb below has a placeholder; fill it in "
            "before sending.",
            err=True,
        )
    if not pack_ready:
        click.echo(
            "warning: the local bootstrap pack is incomplete — the onboarding page will "
            "fail closed with 503; do not send this invitation until all three raw files "
            "and their .sigstore.json bundles are deployed.",
            err=True,
        )
    click.echo("\n--- send this to the collaborator (out of band) ---")
    click.echo(blurb)


@invite.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include revoked + expired invites.")
@click.pass_context
def invite_list(ctx: click.Context, show_all: bool) -> None:
    """List invites, one per line (a11y plain text). Redeemed invites show the bound identity."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    with _open_station(ctx) as st:
        rows = st.store.list_invites(include_inactive=True)
    for r in rows:
        state = _invite_state(r, now)
        # Default view shows outstanding + redeemed (the operator-actionable states);
        # --all also surfaces revoked/expired. Redemptions are visible by default so
        # a hostile bind is directly detectable (INV-4).
        if not show_all and state in ("revoked", "expired"):
            continue
        click.echo(_invite_line(r, state))


@invite.command("revoke")
@click.argument("token_or_hash")
@click.option(
    "--hash",
    "is_hash",
    is_flag=True,
    help="Treat the argument as a token hash (or prefix from 'invite list'), not a plaintext token.",
)
@click.pass_context
def invite_revoke(ctx: click.Context, token_or_hash: str, is_hash: bool) -> None:
    """Revoke an outstanding invite (by plaintext token, or --hash for a hash/prefix).

    A token minted before the leading-dash guard can begin with ``-``, which
    reads as an option; pass such a token after a ``--`` separator
    (``skein station invite revoke -- -TOKEN``), or revoke by ``--hash``."""
    from .identity import hash_token

    with _open_station(ctx) as st:
        token_h = _resolve_invite_hash(st, token_or_hash) if is_hash else hash_token(token_or_hash)
        if not st.store.revoke_invite(token_h):
            raise click.ClickException(f"no active invite to revoke ({token_h[:12]})")
    click.echo(f"revoked invite {token_h[:12]}")


# --- collaborator-side identity + redeem verbs (these DO speak HTTP) ----------


@station.command()
@click.option(
    "--oob",
    "force_oob",
    is_flag=True,
    help="Out-of-band code flow (no local browser, e.g. SSH/headless).",
)
@click.option("--json", "output_json", is_flag=True)
def whoami(force_oob: bool, output_json: bool) -> None:
    """Run the Sigstore login and print your verified identity (issuer + subject).

    Closes the subject-discovery gap for the manual ``account add`` fallback: the
    subject printed here is the email the cert SAN will carry, read straight off the
    OIDC identity token — NO Fulcio cert and NO Rekor entry are created. Diagnostics
    go to stderr so the values capture cleanly.

      skein station whoami           # browser login, prints issuer + subject
      skein station whoami --oob     # SSH/headless code flow
    """
    from . import sign as _sign

    session = _sign.acquire_oidc_session(force_oob=force_oob)
    if output_json:
        _emit_json({"issuer": session.issuer, "subject": session.subject})
        return
    click.echo(f"issuer {session.issuer}")
    click.echo(f"subject {session.subject}")


@station.command()
def login() -> None:
    """Run the interactive Sigstore login; print the OIDC token to stdout.

    The human-accountability gate: opens a browser. The token is short-lived —
    use it promptly. Diagnostics go to stderr so the token captures cleanly:

      TOKEN=$(skein station login) && skein publish --site S --to URL --token "$TOKEN"

    On a headless/SSH box (no local browser, can't capture), use the workbench
    publish verb's inline out-of-band flow instead — its code prompt stays on
    the terminal: skein publish ... --login --oob
    """
    from . import sign as _sign

    provider = _sign.acquire_oidc_provider()
    click.echo(f"signed in as {provider.issuer}", err=True)
    click.echo(provider.token)


@station.command("redeem-invite")
@click.argument("token")
@click.option(
    "--to",
    "instance_url",
    required=True,
    help="Public write host from your invite blurb (e.g. https://ingress.interskein.com).",
)
@click.option("--login", is_flag=True, help="Run the interactive Sigstore login here (required).")
@click.option(
    "--oob", "force_oob", is_flag=True, help="With --login: out-of-band code flow (SSH/headless)."
)
@click.option(
    "--origin",
    default=None,
    help="Station origin the proof binds to (default: the --to value).",
)
@click.option(
    "--yes", is_flag=True, help="Skip the Rekor-consent confirmation (you have already consented)."
)
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def redeem_invite(
    ctx: click.Context,
    token: str,
    instance_url: str,
    login: bool,
    force_oob: bool,
    origin: Optional[str],
    yes: bool,
    output_json: bool,
) -> None:
    """Redeem an invite TOKEN: a token-bound Sigstore ceremony binds you as an author.

    The collaborator side of onboarding. Runs the interactive Sigstore login, signs
    a proof that commits to THIS token + THIS station, and POSTs it to the station,
    which atomically burns the invite and binds your discovered identity.

      skein station redeem-invite <token> --to https://ingress.interskein.com --login

    On a headless box add --oob (out-of-band code flow). Redeeming writes your
    token's hash + your identity to the PUBLIC Rekor transparency log — you are
    asked to confirm first (the human-accountability stop); --yes skips the prompt.

    A token minted before the leading-dash guard can begin with ``-``, which
    reads as an option; pass options first, then the token after a ``--``
    separator (``skein station redeem-invite --to URL --login -- -TOKEN``).
    """
    from . import profile as _profile
    from . import publish as _publish
    from . import sign as _sign

    if not login:
        raise click.ClickException("redeem-invite needs --login (the Sigstore OIDC ceremony).")
    # Canonicalize the signed origin the SAME way the station canonicalizes
    # SKEIN_STATION_ORIGIN (publish.canonical_instance), whether it came from
    # --origin or defaulted to --to, so an explicit but non-canonical --origin
    # still matches. Totality (p4n5 #1): a malformed value is a clean CLI error.
    try:
        origin = _publish.canonical_instance(origin or instance_url)
    except ValueError as e:
        raise click.ClickException(f"invalid origin {(origin or instance_url)!r}: {e}")
    # The Rekor-consent stop (hard human-confirm). Required especially under --oob on
    # a headless box where there is no other human-in-the-loop; abort if declined.
    if not yes:
        click.confirm(
            "Redeeming signs with your identity and writes a record to the PUBLIC "
            "Rekor transparency log (your invite token's hash and your email become "
            "permanently public). Continue?",
            abort=True,
        )
    session = _sign.acquire_oidc_session(force_oob=force_oob)
    signer = _sign.make_oidc_signer(
        session.provider, canon_profile=_profile.CANON_PROFILE_REDEEM_V1
    )
    proof, _issuer, _subject = _sign.sign_redeem_proof(token, origin, signer)
    try:
        status, body = _publish.post_redeem(instance_url, token, proof)
    except _publish.PublishError as e:
        raise click.ClickException(str(e))
    if output_json:
        _emit_json({"http_status": status, **body})
        return
    if body.get("ok"):
        click.echo(f"redeemed — bound as author: {body.get('subject')}")
    else:
        reason = body.get("error") or body.get("status") or "unknown error"
        raise click.ClickException(f"redeem failed ({status}): {reason}")
