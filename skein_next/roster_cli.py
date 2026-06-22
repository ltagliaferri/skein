"""Roster / lifecycle / activity / chain-yield CLI (agent-coordination port, Stage 2).

The daily agent-coordination verbs, re-homed off the legacy client→server HTTP
hop onto the local content-hash :class:`~skein_next.station.Station`:

- ``ignite`` / ``register`` — mint the agent's ``type=agent`` folio in the
  ``roster`` site and guard its name (the new D1 name register).
- ``ready`` / ``torch`` / ``complete`` — drive the lifecycle FSM
  (orienting → active → retiring → retired). ``complete`` also leaves a chain
  yield when ``SKEIN_CHAIN_ID`` is set (the mill hand-off, design D4).
- ``identify`` — set the agent-identity env value for the shell.
- ``roster`` / ``activity`` — the roster list and the authored-folios activity feed.
- ``chain yields`` / ``chain yield`` — read a chain's sack back.

Wired onto the main ``cli`` group the same way Stage 1 attached the shard group
(:func:`skein_next.cli`'s ``cli.add_command``). Output is screen-reader plain text
— one item per line, a description then the id — with ``--json`` for tooling.
"""

from __future__ import annotations

import os
from typing import List, Optional

import click

from .station import (
    AgentNameTaken,
    AmbiguousReference,
    IllegalAgentTransition,
    Station,
    UnknownAgent,
    UnknownSite,
    _short,
)

ENV_AGENT = "SKEIN_NEXT_AGENT"
ENV_CHAIN = "SKEIN_CHAIN_ID"
ENV_CHAIN_TASK = "SKEIN_CHAIN_TASK"


def _open_station(ctx: click.Context) -> Station:
    return Station(ctx.obj.get("data_dir") if ctx.obj else None)


def _default_agent() -> Optional[str]:
    return os.environ.get(ENV_AGENT) or os.environ.get("SKEIN_AGENT")


def _require_agent(agent: Optional[str]) -> str:
    """The agent name/id from --agent or $SKEIN_NEXT_AGENT, or a clear error."""
    resolved = agent or _default_agent()
    if not resolved:
        raise click.ClickException(
            "no agent identity — pass --agent NAME or set $SKEIN_NEXT_AGENT "
            "(run 'ignite' first to get an assigned name)."
        )
    return resolved


def _generate_name(existing: set) -> str:
    """A memorable agent name, avoiding ``existing`` (legacy's generator, reused).

    Falls back to a timestamped id if the legacy generator is unavailable, so a
    fresh station can always ignite even outside a full legacy checkout. The
    fallback carries a random token, NOT just a second-resolution timestamp:
    two cold-start ignites in the same second would otherwise generate the same
    name, and since an auto-name becomes its own agent-id, register_agent would
    read the second as a re-ignite of the first and silently merge the two
    identities rather than rejecting (the disjoint-namespace guard rejects a
    name held under a DIFFERENT agent-id, but two identical auto-names share the
    SAME agent-id and read as a re-ignite). The 64-bit ``token_hex(8)`` makes a
    same-second collision astronomically unlikely rather than guaranteeing none —
    the guarantee is probabilistic, not by-construction. ``existing`` dedups the
    rare in-process repeat, and the caller re-checks the store and retries on the
    guard's rejection, so a collision is recoverable even if it does occur.
    """
    try:
        from skein.utils import generate_agent_name

        return generate_agent_name(existing_names=existing)
    except Exception:
        import secrets
        from datetime import datetime, timezone

        base = "agent-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        while True:
            name = f"{base}-{secrets.token_hex(8)}"
            if name not in existing:
                return name


# --- ignite / register ------------------------------------------------------


@click.command()
@click.option("--message", help="Initial task / mission (recorded on the agent folio).")
@click.option("--mantle", help="Mantle / role name (recorded in agent metadata).")
@click.argument("brief_id", required=False)
@click.option("--name", help="Agent name to claim (default: generated, collision-safe).")
@click.option("--agent", "agent", default=None, help="Agent id (default: $SKEIN_NEXT_AGENT or the name).")
@click.option("--type", "agent_type", default=None, help="Agent type (e.g. claude-code).")
@click.option("--description", default=None, help="Longer description of focus.")
@click.option("--capabilities", default=None, help="Comma-separated capabilities.")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def ignite(
    ctx: click.Context,
    message: Optional[str],
    mantle: Optional[str],
    brief_id: Optional[str],
    name: Optional[str],
    agent: Optional[str],
    agent_type: Optional[str],
    description: Optional[str],
    capabilities: Optional[str],
    output_json: bool,
) -> None:
    """Begin a session: register on the roster as 'orienting'.

    The lifecycle entry point. Mints the agent's roster folio (status orienting)
    and claims its name. After orienting, run 'ready' to go active.
    """
    caps = [c.strip() for c in capabilities.split(",")] if capabilities else []
    metadata = {
        k: v
        for k, v in {
            "ignited_from": brief_id,
            "mantle": mantle,
            "message": message,
        }.items()
        if v
    }
    with _open_station(ctx) as st:
        agent_id = agent or _default_agent() or name
        # Auto-name only when the caller supplied nothing to identify by. The name
        # is generated and bound below; the guarded register is the source of truth
        # for collisions, so an auto-name that loses a race (another ignite claimed
        # it between our snapshot and the register) is regenerated and retried —
        # the point-in-time snapshot in _generate_name is an optimization, not the
        # guarantee.
        auto_named = not name and not agent_id
        attempts = 0
        while True:
            if auto_named:
                existing = {a["name"] for a in st.list_agents()}
                name = _generate_name(existing)
                agent_id = name
            agent_id = agent_id or name
            try:
                folio_hash = st.register_agent(
                    agent_id=agent_id,
                    name=name or agent_id,
                    agent_type=agent_type,
                    description=description or message,
                    capabilities=caps,
                    metadata=metadata,
                )
                break
            except AgentNameTaken as e:
                attempts += 1
                if not auto_named or attempts >= 5:
                    raise click.ClickException(str(e))
                # Auto-name collided with a concurrent register; regenerate.
                continue
        resolved_name = name or agent_id
        if output_json:
            _emit_json(
                {
                    "agent_id": agent_id,
                    "name": resolved_name,
                    "content_hash": folio_hash,
                    "status": st.status_of(folio_hash),
                }
            )
            return
        click.echo("=" * 60)
        click.echo("IGNITION — Orientation Phase")
        click.echo("=" * 60)
        click.echo()
        if message:
            click.echo(f"Mission: {message}")
            click.echo()
        click.echo(f"You are: {resolved_name}")
        click.echo()
        click.echo("After orienting, activate with:")
        click.echo(f"  interskein ready --agent {resolved_name}")


@click.command()
@click.argument("agent_id")
@click.option("--name", help="Human-readable name (default: AGENT_ID).")
@click.option("--type", "agent_type", default=None, help="Agent type.")
@click.option("--description", default=None, help="Longer description.")
@click.option("--capabilities", default=None, help="Comma-separated capabilities.")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def register(
    ctx: click.Context,
    agent_id: str,
    name: Optional[str],
    agent_type: Optional[str],
    description: Optional[str],
    capabilities: Optional[str],
    output_json: bool,
) -> None:
    """Register AGENT_ID on the roster (mint its agent folio + guard its name)."""
    caps = [c.strip() for c in capabilities.split(",")] if capabilities else []
    with _open_station(ctx) as st:
        try:
            folio_hash = st.register_agent(
                agent_id=agent_id,
                name=name or agent_id,
                agent_type=agent_type,
                description=description,
                capabilities=caps,
            )
        except AgentNameTaken as e:
            raise click.ClickException(str(e))
        if output_json:
            _emit_json({"agent_id": agent_id, "name": name or agent_id, "content_hash": folio_hash})
        else:
            click.echo(f"Registered: {name or agent_id} {folio_hash}")


@click.command()
@click.argument("agent_id")
@click.option("--eval", "eval_mode", is_flag=True, help="Print an eval-able export line.")
@click.pass_context
def identify(ctx: click.Context, agent_id: str, eval_mode: bool) -> None:
    """Set your agent identity for this shell session.

    Usage: eval $(interskein identify NAME --eval)
    """
    if eval_mode:
        click.echo(f"export {ENV_AGENT}={agent_id}")
        return
    click.echo(f"To identify as {agent_id}, run:")
    click.echo(f"  export {ENV_AGENT}={agent_id}")


# --- lifecycle transitions --------------------------------------------------


def _transition(ctx: click.Context, agent: Optional[str], verb: str) -> None:
    """Drive one FSM edge, mapping the station's errors to clean CLI errors."""
    ref = _require_agent(agent)
    mover = {"ready": "agent_ready", "torch": "agent_torch"}[verb]
    with _open_station(ctx) as st:
        try:
            pre_hash = st._resolve_agent(ref)
        except UnknownAgent as e:
            raise click.ClickException(str(e) + " — run 'ignite' first.")
        except AmbiguousReference as e:
            lines = "\n".join("  " + _short(m, 24) for m in e.matches)
            raise click.ClickException(f"{e}\n{lines}")
        # Weave the status thread under the agent-id (the folio's created_by, the
        # roster join key), not the raw ref, which may be the human name. The
        # agent-id is incarnation-stable across a re-ignite, so a pre-lock read of
        # it is safe.
        agent_id = (st.store.get_folio(pre_hash) or {}).get("created_by") or ref
        try:
            # The mover re-resolves UNDER the lock and returns the LIVE folio hash.
            # A concurrent same-name re-ignite could supersede pre_hash between our
            # resolve and the transaction, so report status from the hash the
            # transition actually moved — never the stale pre-resolve (finding C).
            live_hash = getattr(st, mover)(ref, by=agent_id)
        except IllegalAgentTransition as e:
            raise click.ClickException(str(e))
        new_status = st.status_of(live_hash)
    click.echo(f"{ref}: {new_status}")


@click.command()
@click.option("--agent", default=None, help="Agent name (default: $SKEIN_NEXT_AGENT).")
@click.pass_context
def ready(ctx: click.Context, agent: Optional[str]) -> None:
    """Activate: orienting → active."""
    _transition(ctx, agent, "ready")


@click.command()
@click.option("--agent", default=None, help="Agent name (default: $SKEIN_NEXT_AGENT).")
@click.pass_context
def torch(ctx: click.Context, agent: Optional[str]) -> None:
    """Begin retirement: active → retiring. File remaining work, then 'complete'."""
    _transition(ctx, agent, "torch")


@click.command()
@click.option("--agent", default=None, help="Agent name (default: $SKEIN_NEXT_AGENT).")
@click.option("--summary", default=None, help="Optional retirement summary.")
@click.option(
    "--yield-status",
    "yield_status",
    type=click.Choice(["complete", "partial", "blocked"]),
    default=None,
    help="Chain yield status (only used when $SKEIN_CHAIN_ID is set).",
)
@click.option("--yield-outcome", "yield_outcome", default=None, help="What was accomplished.")
@click.option("--yield-notes", "yield_notes", default=None, help="Notes for the next task.")
@click.pass_context
def complete(
    ctx: click.Context,
    agent: Optional[str],
    summary: Optional[str],
    yield_status: Optional[str],
    yield_outcome: Optional[str],
    yield_notes: Optional[str],
) -> None:
    """Retire: retiring → retired. Stores a chain yield if $SKEIN_CHAIN_ID is set."""
    ref = _require_agent(agent)
    chain_id = os.environ.get(ENV_CHAIN)
    task_id = os.environ.get(ENV_CHAIN_TASK)
    with _open_station(ctx) as st:
        sack_id = None
        new_status = None
        # The transition, the chain yield, and the summary commit as ONE atomic
        # unit (finding A). The transition alone used to commit in its own
        # transaction; a yield or summary that then failed (store error, lock
        # timeout, or a duplicate sack_id from the legacy yield-id generator) left
        # the agent 'retired' with no yield — and a retry hit IllegalAgentTransition
        # because the FSM had already advanced, so the hand-off was lost
        # unrecoverably. Folding all three into one transaction means any failure
        # rolls the transition back too: the agent stays 'retiring' and the command
        # is cleanly retryable, with no yield stored and no summary minted.
        #
        # Resolving the agent INSIDE this transaction also fixes the stale-resolve
        # (finding C): agent_id and the printed status come from the live folio the
        # transition moved under the lock, not a pre-lock read a concurrent
        # re-ignite could supersede.
        try:
            with st.store.transaction():
                folio_hash = st._resolve_agent(ref)
                # The agent-id (the folio's created_by) is the authorship + query
                # key for the yield, the authored-folios scan, and the summary post
                # — never the raw ref, which may be the human name (register
                # --name X --agent Y).
                agent_id = (st.store.get_folio(folio_hash) or {}).get("created_by") or ref

                # Lifecycle transition FIRST, via the lock-held core so it shares
                # this transaction. The yield/summary are durable side effects;
                # running them before the FSM guard would leave them behind on an
                # illegal complete (e.g. from 'active', skipping torch). The guard
                # rejecting here rolls the whole batch back — a clean no-op, no
                # duplicate yields stacked on a retry.
                live_hash = st._transition_agent_locked(folio_hash, "retired", by=agent_id)
                new_status = st.status_of(live_hash)
                agent_id = (st.store.get_folio(live_hash) or {}).get("created_by") or agent_id

                authored = [
                    f
                    for f in st.store.folios_by_created_by(agent_id)
                    if f.get("type") not in ("agent", "site")
                ]

                if chain_id:
                    artifacts = [f["content_hash"] for f in authored]
                    tenders = [
                        f["content_hash"] for f in authored if f.get("type") == "tender"
                    ]
                    status = yield_status or ("complete" if tenders else "partial")
                    outcome = (
                        yield_outcome
                        or f"Completed task. Filed {len(artifacts)} artifact(s)."
                    )
                    sack_id = st.store_chain_yield(
                        chain_id=chain_id,
                        task_id=task_id or "unknown",
                        agent_id=agent_id,
                        status=status,
                        outcome=outcome,
                        artifacts=artifacts,
                        notes=yield_notes,
                        tender_id=tenders[0] if tenders else None,
                    )

                if summary:
                    # Post to the agent's most-recent working site (legacy
                    # behavior), falling back to the roster site.
                    recent = sorted(
                        authored,
                        key=lambda f: (f.get("created_at") or "", f.get("content_hash") or ""),
                        reverse=True,
                    )
                    site = None
                    for f in recent:
                        site = st.store.folio_site_slug(f["content_hash"])
                        if site:
                            break
                    target = site or "roster"
                    try:
                        st.post(type="summary", site=target, title=summary[:100],
                                 content=summary, created_by=agent_id)
                    except UnknownSite:
                        # The chosen working site no longer resolves to a site; fall
                        # back to the roster (which the register guaranteed exists).
                        # Only the missing-site case is swallowed — any other store
                        # error surfaces (and now rolls the transition back too).
                        if target == "roster":
                            raise
                        st.post(type="summary", site="roster", title=summary[:100],
                                 content=summary, created_by=agent_id)
        except UnknownAgent as e:
            raise click.ClickException(str(e) + " — run 'ignite' first.")
        except AmbiguousReference as e:
            lines = "\n".join("  " + _short(m, 24) for m in e.matches)
            raise click.ClickException(f"{e}\n{lines}")
        except IllegalAgentTransition as e:
            raise click.ClickException(str(e))

    if sack_id:
        click.echo(f"Yield stored: {sack_id}")
    click.echo(f"{ref}: {new_status}")


# --- roster / activity ------------------------------------------------------


@click.command()
@click.option("--status", default=None, help="Filter by lifecycle status.")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def roster(ctx: click.Context, status: Optional[str], output_json: bool) -> None:
    """List registered agents (one per name) with their lifecycle status."""
    with _open_station(ctx) as st:
        agents = st.list_agents(status=status)
        if output_json:
            _emit_json(
                [
                    {
                        "name": a["name"],
                        "agent_id": a.get("created_by"),
                        "status": a["status"],
                        "agent_type": (a.get("meta") or {}).get("agent_type"),
                        "content_hash": a["content_hash"],
                    }
                    for a in agents
                ]
            )
            return
        if not agents:
            click.echo("no agents registered")
            return
        for a in agents:
            atype = (a.get("meta") or {}).get("agent_type")
            type_part = f" [{atype}]" if atype else ""
            click.echo(f"{a['name']} — {a['status']}{type_part} {_short(a['content_hash'])}")


@click.command()
@click.option("--status", default=None, help="Filter by lifecycle status.")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def activity(ctx: click.Context, status: Optional[str], output_json: bool) -> None:
    """The roster activity feed: each agent's authored-folio activity, recent first."""
    with _open_station(ctx) as st:
        feed = st.agent_activity(status=status)
        if output_json:
            _emit_json(feed)
            return
        if not feed:
            click.echo("no agents registered")
            return
        for a in feed:
            site_part = f", site {a['working_site']}" if a["working_site"] else ""
            click.echo(
                f"{a['name']} — {a['status']}, {a['activity_status']}, "
                f"{a['folio_count']} folios{site_part} {_short(a['content_hash'])}"
            )


# --- chain yields -----------------------------------------------------------


@click.group()
def chain() -> None:
    """Read a chain's sack (the mill hand-off yields)."""


@chain.command("yields")
@click.argument("chain_id")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def chain_yields(ctx: click.Context, chain_id: str, output_json: bool) -> None:
    """List all yields in CHAIN_ID, in execution order."""
    with _open_station(ctx) as st:
        items = st.chain_yields(chain_id)
        if output_json:
            _emit_json(items)
            return
        if not items:
            click.echo(f"no yields for chain {chain_id}")
            return
        for y in items:
            click.echo(
                f"{y.get('status') or '?'}: {y.get('outcome') or ''} "
                f"(task {y.get('task_id')}, by {y.get('agent_id')}) {y['sack_id']}"
            )


@chain.command("yield")
@click.argument("sack_id")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def chain_yield(ctx: click.Context, sack_id: str, output_json: bool) -> None:
    """Show one yield by its SACK_ID."""
    with _open_station(ctx) as st:
        y = st.chain_yield(sack_id)
        if not y:
            raise click.ClickException(f"no yield {sack_id!r}")
        if output_json:
            _emit_json(y)
            return
        click.echo(f"{y['sack_id']}  chain {y.get('chain_id')}  task {y.get('task_id')}")
        if y.get("agent_id"):
            click.echo(f"by {y['agent_id']}")
        if y.get("status"):
            click.echo(f"status: {y['status']}")
        if y.get("outcome"):
            click.echo(f"outcome: {y['outcome']}")
        if y.get("artifacts"):
            click.echo("artifacts: " + ", ".join(y["artifacts"]))
        if y.get("notes"):
            click.echo(f"notes: {y['notes']}")


def _emit_json(value) -> None:
    import json as _json

    click.echo(_json.dumps(value, indent=2, ensure_ascii=False, default=str))


COMMANDS: List[click.Command] = [
    ignite,
    register,
    identify,
    ready,
    torch,
    complete,
    roster,
    activity,
    chain,
]
