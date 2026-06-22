"""Station service layer over the content-hash store (Slice 3).

The store (:mod:`skein_next.store`) is pure storage: it hashes, inserts, and
queries rows. The *station* is the layer above it that speaks in the daily verbs
— post a folio into a site, read a folio by a short reference, list a site's
folios, search, walk a folio's thread graph, list sites. The CLI (Slice 3) and
the web read adapter (Slice 4) both sit on this one seam so the two surfaces can
never drift apart.

Three content-hash facts shape this layer:

- A folio's identity is its ``sha256::<hex>`` address. There is no human folio
  id, so :meth:`Station.resolve_ref` accepts a full address, a git-style short
  prefix, or a legacy id (via the alias table) and collapses them to one hash.
- A site is itself a ``type=site`` folio; membership is a ``within`` thread from
  the member folio to the site folio. ``folios_in_site`` walks that, not a
  column.
- Status is not a column either; it is thread-derived. This layer does not
  invent one — it exposes folios and threads and lets the caller read status off
  the graph.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from skein import address

from .agent import parse_agent_meta, render_agent_content
from .store import SkeinNextStore

# A bare ``sha256::<hex>`` short-hash reference, 8..63 hex digits. The hardened
# `::` grammar rejects a *bare* short hash (it has no station to disambiguate
# against), so the local station resolves this convenience form itself — through
# the same StationIndex prefix lookup the resolver uses, not a naive scan. The
# 8-digit floor matches the addressing scheme's minimum short-hash length.
_LOCAL_SHORT_HASH = re.compile(r"^sha256::([0-9a-f]{8,63})$")
# Cap on candidates fetched for a short-hash prefix; enough to detect ambiguity.
_PREFIX_CANDIDATE_CAP = 100


_AMBIGUOUS_CAP = 10  # most candidates resolve_ref fetches/shows for an ambiguous prefix


class AmbiguousReference(Exception):
    """A short hash prefix matched more than one folio.

    ``matches`` is capped at ``_AMBIGUOUS_CAP`` candidates; ``capped`` records
    whether more existed, so the message says "at least N" rather than reporting
    a floor as if it were exact.
    """

    def __init__(self, prefix: str, matches: List[str]):
        self.prefix = prefix
        self.capped = len(matches) > _AMBIGUOUS_CAP
        self.matches = matches[:_AMBIGUOUS_CAP]
        count = f"at least {len(self.matches)}" if self.capped else str(len(self.matches))
        super().__init__(f"reference {prefix!r} is ambiguous ({count} folios match)")


class UnknownSite(Exception):
    """A site slug has no site folio in this station."""

    def __init__(self, slug: str):
        self.slug = slug
        super().__init__(f"no site named {slug!r}")


class UnknownFolio(Exception):
    """A reference did not resolve to any folio in this station."""

    def __init__(self, ref: str):
        self.ref = ref
        super().__init__(f"no folio for reference {ref!r}")


class UnknownAgent(Exception):
    """A name/reference did not resolve to a ``type=agent`` folio."""

    def __init__(self, ref: str):
        self.ref = ref
        super().__init__(f"no agent named {ref!r}")


class AgentNameTaken(Exception):
    """A name is already held by a DIFFERENT agent (the D1 name guard fired).

    ``holder`` is the agent-id (``created_by``) of the agent currently holding the
    name, when known — so the rejection can name who owns it.
    """

    def __init__(self, name: str, holder: Optional[str] = None):
        self.name = name
        self.holder = holder
        who = f" (held by {holder})" if holder else ""
        super().__init__(f"agent name {name!r} is already taken{who}")


class AgentHandleConflict(AgentNameTaken):
    """A registration's name or agent-id collides with the OTHER kind of handle
    already held by a DIFFERENT agent (the disjoint-namespace guard, finding B).

    :meth:`Station._resolve_agent` tries the name slug BEFORE the agent-id, so if
    agent X's name equalled agent Y's agent-id, the handle ``X`` would always
    resolve to X and mask Y — ``identify`` could emit Y's id while ``ready`` /
    ``complete`` operated on X's folio. Registration forbids the crossover: a name
    may not equal a different live agent's agent-id, nor an agent-id a different
    live agent's name. With the namespaces disjoint, ``_resolve_agent``'s path
    order is irrelevant and the identify round-trip is airtight.

    Subclasses :class:`AgentNameTaken` so existing ``except AgentNameTaken``
    handlers (ignite/register) still catch it; ``kind`` is which side collided
    (``"name"`` or ``"agent-id"``).
    """

    def __init__(self, handle: str, kind: str, holder: Optional[str] = None):
        self.handle = handle
        self.name = handle
        self.kind = kind
        self.holder = holder
        who = f" (held by {holder})" if holder else ""
        Exception.__init__(
            self,
            f"agent handle {handle!r} collides with a different agent's {kind}{who}",
        )


class SlugCollision(Exception):
    """A slug is already bound to a NON-site folio (e.g. a Stage-2 agent name),
    so it cannot be (re)used as a site slug. The create-site half of the D1 name
    guard: reject rather than return the colliding non-site folio as if it were a
    site (finding-20260621-ccmy)."""

    def __init__(self, slug: str):
        self.slug = slug
        super().__init__(
            f"slug {slug!r} is already in use by a non-site folio (e.g. an agent name)"
        )


class IllegalAgentTransition(Exception):
    """A requested lifecycle move is not a legal edge of the agent FSM.

    Lifecycle is a strict chain ``orienting → active → retiring → retired``; any
    other move (skipping a state, going backwards, or re-entering the current
    state — e.g. running ``ready`` twice) raises this. ``current``/``requested``
    carry the offending pair.
    """

    def __init__(self, current: str, requested: str):
        self.current = current
        self.requested = requested
        super().__init__(
            f"illegal lifecycle transition {current!r} → {requested!r}"
        )


# The reserved roster site: every agent is a ``type=agent`` folio that lives here.
ROSTER_SITE = "roster"

# The agent lifecycle FSM. A strict linear chain — an agent orients, goes active,
# begins retiring (torch), then retires (complete). The guard admits only these
# edges; a state is never legal to re-enter (so ``ready`` on an already-active
# agent rejects, matching legacy, which errored "already active").
AGENT_LIFECYCLE = ("orienting", "active", "retiring", "retired")
_LEGAL_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "orienting": ("active",),
    "active": ("retiring",),
    "retiring": ("retired",),
    "retired": (),
}
# The first status an agent folio carries, written at registration.
_INITIAL_STATUS = "orienting"
# Legacy's "active" activity window: a folio authored within this many minutes.
ACTIVE_WINDOW_MINUTES = 30
# Folio types that are roster/infrastructure, NOT authored work. Legacy's folio
# store never held sites or agents (separate stores), so its activity aggregate
# counted neither; the unified content-hash store keeps them as folios, so the
# activity feed filters them back out to stay faithful (a fresh agent that has
# only its own agent folio + the roster site it minted still reads "idle").
_NON_WORK_FOLIO_TYPES = frozenset({"agent", "site"})


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse a stored ISO ``created_at`` to an aware UTC datetime, or ``None``.

    A naive stamp is assumed UTC (mirroring the store's redeem-window parse), so
    the recency comparison in the activity feed can't raise on aware-vs-naive.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _short(content_hash: str, length: int = 12) -> str:
    """A git-style short address: ``sha256::`` + the first ``length`` hex chars."""
    if content_hash.startswith("sha256::"):
        return "sha256::" + content_hash[len("sha256::") :][:length]
    return content_hash[:length]


def _title_line(text: Optional[str], limit: int = 100) -> str:
    """The first non-blank line of ``text``, trimmed and length-capped."""
    if not text:
        return ""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(first) > limit:
        first = first[: limit - 1] + "…"
    return first


class Station:
    """The daily-verb service over a content-hash store. Context-manageable."""

    def __init__(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        check_same_thread: bool = True,
    ):
        self.store = SkeinNextStore(data_dir, check_same_thread=check_same_thread)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Station":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- references (StationIndex + :: resolution) --------------------------

    def folios_with_prefix(self, algo: str, prefix: str) -> List[str]:
        """:class:`skein.address.StationIndex` hook: full digests for a prefix.

        Returns the matching FULL lowercase-hex digests (no ``sha256::`` framing)
        for folios whose hash begins with ``prefix``. This is what the hardened
        ``::`` resolver calls to lengthen a short hash against this station.
        """
        if algo != "sha256":
            return []
        framed = self.store.find_by_prefix(f"{algo}::{prefix}", limit=_PREFIX_CANDIDATE_CAP)
        return [h.split("::", 1)[1] for h in framed]

    def resolve_ref(self, ref: str) -> Optional[str]:
        """Resolve a user-supplied reference to a full content hash, or ``None``.

        Resolution order, first hit wins:
        1. an exact stored ``sha256::<hex>`` address (fast path);
        2. a legacy id present in the alias table;
        3. a structurally-valid ``::`` address (bare hash, or alias/web form
           whose short hash this station lengthens) routed through the hardened
           ``skein.address`` parser and resolver;
        4. the local bare short-hash convenience ``sha256::<8..63 hex>``, which
           the ``::`` grammar rejects as a bare short hash — resolved here via
           the same StationIndex prefix lookup the resolver uses.

        Raises :class:`AmbiguousReference` when a short hash matches several
        folios (translated from the resolver's ``AmbiguousShortHash``).
        """
        if ref.startswith("sha256::") and self.store.get_folio(ref):
            return ref
        aliased = self.store.resolve_alias(ref)
        if aliased:
            return aliased

        try:
            parsed = address.parse(ref)
        except address.AddressError:
            return self._resolve_local_short(ref)

        try:
            digest = address.resolve(parsed, self)
        except address.AmbiguousShortHash as e:
            raise AmbiguousReference(ref, [f"sha256::{c}" for c in e.candidates])
        except address.ShortHashNotFound:
            return None
        framed = f"sha256::{digest}"
        return framed if self.store.get_folio(framed) else None

    def _resolve_local_short(self, ref: str) -> Optional[str]:
        """Resolve a bare ``sha256::<8..63 hex>`` ref against this station."""
        m = _LOCAL_SHORT_HASH.match(ref)
        if not m:
            return None
        matches = self.folios_with_prefix("sha256", m.group(1))
        if len(matches) == 1:
            return f"sha256::{matches[0]}"
        if len(matches) > 1:
            raise AmbiguousReference(ref, [f"sha256::{c}" for c in matches])
        return None

    # --- sites --------------------------------------------------------------

    def _resolve_site_hash(self, slug: str) -> Optional[str]:
        """Resolve a slug to its hash, but only if it points at a ``type=site``
        folio. ``None`` otherwise.

        A site is by definition a ``type=site`` folio, and the slug table is not
        site-exclusive (agent names are slugged too), so a slug resolving to a
        non-site folio is not a site. Every site-facing verb routes through this
        so the "a site is a type=site folio" invariant holds uniformly across the
        write (:meth:`post`) and read (:meth:`get_site`, :meth:`folios_in_site`,
        :meth:`list_sites`) surface.
        """
        site_hash = self.store.resolve_slug(slug)
        if not site_hash:
            return None
        folio = self.store.get_folio(site_hash)
        if not folio or folio.get("type") != "site":
            return None
        return site_hash

    # --- write --------------------------------------------------------------

    def post(
        self,
        type: str,
        site: str,
        title: str,
        content: Optional[str] = None,
        created_by: Optional[str] = None,
        created_at: Any = None,
    ) -> str:
        """Create a folio and attach it to ``site`` with a ``within`` thread.

        ``site`` is a slug that must already resolve to a ``type=site`` folio
        (raises :class:`UnknownSite` otherwise — the station never silently
        invents a site, and never attaches a folio "within" a non-site). The
        slug table is not site-exclusive — agent names are slugged too — so a
        slug resolving to a non-site folio is rejected here, mirroring the legacy
        server, which validated the target was a real site before writing.
        Returns the new folio's content hash. Idempotent: re-posting identical
        fields into the same site yields the same hash and the same single
        membership edge.
        """
        site_hash = self._resolve_site_hash(site)
        if not site_hash:
            raise UnknownSite(site)
        folio_hash = self.store.create_folio(
            {
                "type": type,
                "title": title,
                "content": content,
                "created_at": created_at if created_at is not None else _now_utc(),
                "created_by": created_by,
            }
        )
        self.store.save_thread(from_id=folio_hash, to_id=site_hash, type="within")
        return folio_hash

    def set_status(
        self,
        ref: str,
        status: str,
        by: Optional[str] = None,
        created_at: Any = None,
    ) -> str:
        """Set a folio's status by writing a ``status`` thread. Returns the
        resolved folio hash (the subject the status was written to).

        Status is thread-derived: the latest ``status`` thread (by created_at,
        then thread_hash) wins, matching how the import bridge carried legacy
        statuses and how the read side reads them. The thread is a self-loop
        (``from_id == to_id == folio``, ``weaver`` = author, ``content`` = the
        status word) — the exact shape legacy status threads have. ``created_at``
        defaults to now so a fresh status supersedes earlier ones.

        Raises :class:`UnknownFolio` if ``ref`` does not resolve, or
        :class:`AmbiguousReference` if it is an ambiguous short hash.
        """
        folio_hash = self.resolve_ref(ref)
        if not folio_hash:
            raise UnknownFolio(ref)
        self.store.save_thread(
            from_id=folio_hash,
            to_id=folio_hash,
            type="status",
            weaver=by,
            content=status,
            created_at=created_at if created_at is not None else _now_utc(),
        )
        return folio_hash

    def status_of(self, content_hash: str) -> str:
        """The folio's current status: the latest ``status`` thread's content,
        or ``'open'`` when the folio has no status thread.

        Open is the default — a folio is open until a status thread says
        otherwise — matching legacy SKEIN (whose unset status reads as 'open')
        and the web adapter, which already defaults the same way. Without this
        default the CLI showed no status for the ~80% of folios that are open by
        default, where legacy shows 'open'.
        """
        return self.store.latest_statuses([content_hash]).get(content_hash, "open")

    # --- publish state (client-side, thread-derived) ------------------------
    #
    # A folio is ``local`` by default and becomes ``published`` once it has been
    # pushed to an instance. Like status, this is not a column: publishing
    # records a ``published`` thread from the folio to the instance it went to.
    # The thread is CLIENT-LOCAL bookkeeping — it is never sent over the wire,
    # because an instance knows a folio is published simply by holding it. Two
    # consequences fall out for free: a folio can be published to several
    # instances (one edge each), and re-publishing is idempotent (the edge's
    # content hash dedups).

    PUBLISHED_THREAD = "published"

    def record_published(
        self,
        content_hash: str,
        instance: str,
        by: Optional[str] = None,
        created_at: Any = None,
    ) -> str:
        """Mark a folio published to ``instance`` by writing a ``published`` thread.

        ``instance`` is the target's identifier (its publish URL for now). The
        edge runs folio -> instance; ``content`` repeats the instance id so the
        target reads off the edge without a join. Returns the thread hash.
        """
        return self.store.save_thread(
            from_id=content_hash,
            to_id=instance,
            type=self.PUBLISHED_THREAD,
            weaver=by,
            content=instance,
            created_at=created_at if created_at is not None else _now_utc(),
        )

    def published_instances(self, content_hash: str) -> List[str]:
        """Instances this folio has been published to (``published`` edge targets).

        Deduplicated, in first-published order. Re-publishing to the same
        instance writes a fresh timestamped edge (publish history is kept
        append-only, like status), so the raw edge list can repeat an instance;
        callers asking "where does this live" want the distinct set.
        """
        seen: List[str] = []
        for t in self.store.get_threads(from_id=content_hash, type=self.PUBLISHED_THREAD):
            target = t.get("to_id")
            if target and target not in seen:
                seen.append(target)
        return seen

    def publish_state_of(self, content_hash: str) -> str:
        """``'published'`` if the folio has any ``published`` edge, else ``'local'``.

        Local is the default — a folio exists on the client unpublished until a
        publish records otherwise, mirroring how ``status_of`` defaults to open.
        """
        return "published" if self.published_instances(content_hash) else "local"

    def create_site(
        self,
        slug: str,
        purpose: Optional[str] = None,
        created_by: Optional[str] = None,
        created_at: Any = None,
    ) -> str:
        """Ensure a ``type=site`` folio exists for ``slug``. Returns its hash.

        Idempotent on the slug: if the slug already resolves to a ``type=site``
        folio, that folio is returned untouched and ``purpose``/``created_by`` are
        ignored. This matters because a site folio's hash includes its
        ``created_at`` — re-minting on every call would remap the slug to a fresh
        hash each time and orphan every ``within`` membership edge pointing at the
        old one. Changing a site's purpose is a deliberate update, not a re-create.

        Raises :class:`SlugCollision` if the slug is already bound to a NON-site
        folio. The slug table stopped being site-exclusive in Stage 2 (agent names
        are slugged too), so a bare "already resolves → return it" short-circuit
        would hand back a non-site folio's hash as if it were a site — a non-site
        masquerading as a site at the create boundary (finding-20260621-ccmy). The
        policy is reject, consistent with D1's name guard; reuse the shared
        :meth:`_resolve_site_hash` to tell a real site from a collision.

        Creation is atomic. A site folio's hash includes its ``created_at``, so two
        cold-start agents racing to mint the same reserved slug (the ``roster``
        site at first register) would each compute a DIFFERENT site folio, and a
        last-write-wins ``set_slug`` would leave the loser attached to an orphaned
        site the slug no longer names (finding: concurrent first-register splits
        the roster). So mint + bind run under one ``transaction()`` (``BEGIN
        IMMEDIATE``) with the guarded :meth:`~SkeinNextStore._register_slug_locked`
        CAS, re-resolving the slug under the write lock: the first writer binds its
        folio; the second sees the bound slug and adopts that one site, minting no
        rival folio. The fast path (slug already a site) skips the lock entirely.
        """
        existing = self.store.resolve_slug(slug)
        if existing:
            if self._resolve_site_hash(slug):
                return existing
            raise SlugCollision(slug)
        with self.store.transaction():
            # Re-resolve under the write lock: a concurrent first-create may have
            # bound the slug between our pre-check and acquiring the lock. If so,
            # adopt that site (idempotent) or reject a non-site collision —
            # never mint a rival site folio that would orphan its members.
            existing = self.store.resolve_slug(slug)
            if existing:
                folio = self.store.get_folio(existing)
                if folio and folio.get("type") == "site":
                    return existing
                raise SlugCollision(slug)
            site_hash = self.store.create_folio(
                {
                    "type": "site",
                    "title": slug,
                    "content": purpose,
                    "created_at": created_at if created_at is not None else _now_utc(),
                    "created_by": created_by,
                }
            )
            if not self.store._register_slug_locked(slug, site_hash):
                # Lost the bind under the lock (defensive — BEGIN IMMEDIATE
                # serializes writers, so the re-resolve above normally catches
                # this): adopt whoever won if it is a real site.
                bound = self.store.resolve_slug(slug)
                folio = self.store.get_folio(bound) if bound else None
                if folio and folio.get("type") == "site":
                    return bound
                raise SlugCollision(slug)
            return site_hash

    # --- read ---------------------------------------------------------------

    def get_folio(self, ref: str) -> Optional[Dict[str, Any]]:
        """Read a folio by any reference :meth:`resolve_ref` accepts."""
        content_hash = self.resolve_ref(ref)
        return self.store.get_folio(content_hash) if content_hash else None

    def list_folios(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        return self.store.list_folios(limit=limit, offset=offset)

    def folios_in_site(
        self, site: str, type: Optional[str] = None, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Folios that are members of ``site`` (via ``within`` threads).

        Raises :class:`UnknownSite` if the slug is unknown or names a non-site
        folio. Optionally filtered to one folio ``type``. The store does the
        membership join, ordering by ``created_at`` *before* applying ``limit``
        so the window is the stable earliest-N, not an arbitrary slice.
        ``limit=None`` returns all members.
        """
        site_hash = self._resolve_site_hash(site)
        if not site_hash:
            raise UnknownSite(site)
        return self.store.folios_in_site(site_hash, type=type, limit=limit)

    def search(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self.store.search_folios(query, limit=limit)

    def folios_by_type(self, type: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Station-wide list of folios of one ``type`` (oldest-first).

        The local equivalent of legacy ``GET /folios?type=<type>`` — used by the
        shard ``triage``/``inspect`` verbs to gather tender folios across the
        station, not within a single site.
        """
        return self.store.folios_by_type(type, limit=limit)

    def list_sites(self) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
        """All ``(slug, site_folio)`` pairs for real sites, ordered by slug.

        A non-site slug (e.g. a Stage-2 agent name) is not a site and is omitted.
        ``site_folio`` is ``None`` only if the slug table points at a hash with
        no folio row (a corrupt station) — that case is still surfaced rather
        than hidden, since it can't be told apart from a site by its (missing)
        type and silently dropping it would hide real corruption.
        """
        out: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        for slug, site_hash in self.store.list_slugs():
            folio = self.store.get_folio(site_hash)
            if folio is not None and folio.get("type") != "site":
                continue
            out.append((slug, folio))
        return out

    def get_site(self, slug: str) -> Optional[Dict[str, Any]]:
        site_hash = self._resolve_site_hash(slug)
        return self.store.get_folio(site_hash) if site_hash else None

    def thread_graph(self, ref: str) -> Optional[Dict[str, Any]]:
        """A folio's thread neighborhood: its outgoing and incoming edges.

        Returns ``None`` if the reference does not resolve. Each edge is the raw
        stored thread row plus a ``peer`` field describing the *other* endpoint:
        a resolved folio (when the peer is a stored content hash), or the raw
        actor/unresolved string the bridge kept verbatim. ``within`` membership
        edges are reported under their own key so callers can separate
        membership from substantive links.
        """
        content_hash = self.resolve_ref(ref)
        if not content_hash:
            return None
        focus = self.store.get_folio(content_hash)

        def describe_peer(peer_id: Optional[str]) -> Dict[str, Any]:
            if peer_id is None:
                return {"kind": "none", "id": None, "folio": None}
            folio = self.store.get_folio(peer_id)
            if folio:
                return {"kind": "folio", "id": peer_id, "folio": folio}
            return {"kind": "ref", "id": peer_id, "folio": None}

        outgoing, incoming, memberships = [], [], []
        for edge in self.store.get_threads(from_id=content_hash):
            row = dict(edge)
            row["peer"] = describe_peer(edge["to_id"])
            (memberships if edge["type"] == "within" else outgoing).append(row)
        for edge in self.store.get_threads(to_id=content_hash):
            row = dict(edge)
            row["peer"] = describe_peer(edge["from_id"])
            (memberships if edge["type"] == "within" else incoming).append(row)

        return {
            "folio": focus,
            "content_hash": content_hash,
            "outgoing": outgoing,
            "incoming": incoming,
            "memberships": memberships,
        }

    # --- roster + agent lifecycle (the agent-coordination port, Stage 2) ------
    #
    # The legacy server held one genuinely irreplaceable thing — a place for
    # concurrent agents to see each other (the roster). Even that it reconstructed
    # from folios: "active" was "authored a folio in the last 30 min", not a
    # heartbeat. So the roster re-homes onto folios with no new storage concept:
    # each agent is a ``type=agent`` folio in the reserved ``roster`` site; its
    # lifecycle rides status threads (the same latest-thread-wins mechanism every
    # folio uses); its descriptive fields live in the folio content envelope
    # (:mod:`skein_next.agent`); and the join key is ``created_by`` — the agent-id
    # the agent folio AND every folio the agent authors both carry.
    #
    # Two pieces are genuinely new code, not free reuse: the guarded name register
    # (``set_slug`` is last-write-wins and would let a second agent silently steal
    # an in-use name) and the lifecycle FSM guard (only legal edges admitted).

    def ensure_roster_site(self, created_by: Optional[str] = None) -> str:
        """Ensure the reserved ``roster`` site exists; return its folio hash.

        Idempotent (it is just :meth:`create_site` on the reserved slug). Called at
        the head of every register so a fresh station is usable without a manual
        ``site create roster`` first.
        """
        return self.create_site(
            ROSTER_SITE,
            purpose="Agent roster — lifecycle, presence, and succession.",
            created_by=created_by,
        )

    def register_agent(
        self,
        agent_id: str,
        name: Optional[str] = None,
        agent_type: Optional[str] = None,
        description: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_at: Any = None,
    ) -> str:
        """Register an agent: mint its ``type=agent`` folio + guard its name.

        ``agent_id`` is the lightweight env identity (e.g. ``$SKEIN_NEXT_AGENT``)
        that becomes the folio's ``created_by`` and the activity join key; ``name``
        is the human handle registered as a guarded slug (defaults to ``agent_id``,
        the legacy posture where the two coincide). The descriptive fields ride in
        the folio content envelope. Returns the agent folio's hash; the initial
        lifecycle status is ``orienting``.

        The mint, the membership edge, the name guard, and the initial status all
        commit in ONE ``transaction()`` — the single-writer roster-write discipline
        (design §5): a rejected name leaves NO partial agent folio behind. The name
        guard (design D1):

        - name free  → bound to this folio (the conditional-insert CAS).
        - name held by THIS agent's prior folio (a re-ignite — a new ``created_at``
          mints a new hash) → the slug is rebound to the new incarnation and a
          ``succession`` edge threads back to the prior folio (the live-path
          succession behavior; the dead-code ignite is not resurrected).
        - name held by a DIFFERENT agent → :class:`AgentNameTaken` (rollback). A
          second live agent must not steal an in-use name.
        """
        if not agent_id:
            raise ValueError("register_agent requires a non-empty agent_id")
        name = name or agent_id
        site_hash = self.ensure_roster_site(created_by=agent_id)
        meta: Dict[str, Any] = {
            "agent_id": agent_id,
            "name": name,
            "agent_type": agent_type,
            "description": description,
            "capabilities": list(capabilities) if capabilities else [],
            "metadata": dict(metadata) if metadata else {},
        }
        content = render_agent_content(description or name, meta)
        stamp = created_at if created_at is not None else _now_utc()
        fields = {
            "type": "agent",
            "title": name,
            "content": content,
            "created_at": stamp,
            "created_by": agent_id,
        }
        with self.store.transaction():
            # Disjoint-namespace guard (finding B): a name must not equal a
            # DIFFERENT live agent's agent-id, and an agent-id must not equal a
            # different agent's name. _resolve_agent tries the name slug before
            # the agent-id, so a crossover would mask one agent behind the other.
            # Every check excludes THIS agent's own folios, so the auto-ignite
            # case (name == own agent-id) and a same-agent re-ignite (same id,
            # same name) stay allowed — only a different agent claiming a handle
            # already in use as the other kind is rejected.
            other_agent_ids = {
                f.get("created_by")
                for f in self.store.folios_by_type("agent")
                if f.get("created_by") and f.get("created_by") != agent_id
            }
            if name in other_agent_ids:
                raise AgentHandleConflict(name, "agent-id", holder=name)
            for slug, slug_hash in self.store.list_slugs():
                if slug != agent_id:
                    continue
                holder_folio = self.store.get_folio(slug_hash)
                if (
                    holder_folio
                    and holder_folio.get("type") == "agent"
                    and holder_folio.get("created_by") != agent_id
                ):
                    raise AgentHandleConflict(
                        agent_id, "name", holder=holder_folio.get("created_by")
                    )
            prior_hash = self.store.resolve_slug(name)
            folio_hash = self.store.create_folio(fields)
            self.store.save_thread(from_id=folio_hash, to_id=site_hash, type="within")
            if prior_hash and prior_hash != folio_hash:
                prior = self.store.get_folio(prior_hash)
                holder = prior.get("created_by") if prior else None
                # Rebind is allowed ONLY for this agent's own prior AGENT folio (a
                # re-ignite). A name held by a different agent — or by a NON-agent
                # folio this agent happens to have created (e.g. a site it minted) —
                # is a collision: reject, never clobber. This keeps the guard
                # symmetric with create_site, which refuses a non-site slug.
                own_prior_agent = (
                    bool(prior)
                    and prior.get("type") == "agent"
                    and holder == agent_id
                )
                if not own_prior_agent:
                    raise AgentNameTaken(name, holder)  # rollback — no partial folio
                # Same agent, fresh incarnation: deliberate rebind (we own it) plus
                # a succession edge to the prior folio for continuity.
                self.store.set_slug(name, folio_hash)
                self.store.save_thread(
                    from_id=folio_hash,
                    to_id=prior_hash,
                    type="succession",
                    weaver=agent_id,
                )
            elif not self.store._register_slug_locked(name, folio_hash):
                # Free/ours per the pre-read, yet the guarded bind refused — a
                # concurrent writer claimed it first under the lock. Treat as taken.
                raise AgentNameTaken(name, None)
            # Initial lifecycle status. created_at pinned to the folio stamp so a
            # byte-identical re-registration collapses to the same status thread.
            self.store.save_thread(
                from_id=folio_hash,
                to_id=folio_hash,
                type="status",
                weaver=agent_id,
                content=_INITIAL_STATUS,
                created_at=stamp,
            )
        return folio_hash

    def _resolve_agent(self, ref: str) -> str:
        """Resolve a name (slug), content reference, OR agent-id to an agent folio.

        Three resolution paths, in daily-case order:

        1. the name slug (agents are addressed by their handle);
        2. any content reference :meth:`resolve_ref` accepts (a hash/alias);
        3. the agent-id — a ``created_by`` lookup against ``type=agent`` folios.

        Path 3 matters because the agent-id and the name need not coincide
        (``register --name X --agent Y``), and ``identify --eval`` emits the
        agent-id; without it that emitted value would not round-trip back through
        the lifecycle verbs. When several agent folios share the agent-id (a
        re-ignite mints a fresh folio each time), the live incarnation is the one a
        slug currently points at; failing that, the newest by ``created_at``.
        Raises :class:`UnknownAgent` if nothing resolves to an agent folio.
        """
        slug_hash = self.store.resolve_slug(ref)
        if slug_hash:
            folio = self.store.get_folio(slug_hash)
            if folio and folio.get("type") == "agent":
                return slug_hash
        ref_hash = self.resolve_ref(ref)
        if ref_hash:
            folio = self.store.get_folio(ref_hash)
            if folio and folio.get("type") == "agent":
                return ref_hash
        candidates = [
            f
            for f in self.store.folios_by_created_by(ref)
            if f.get("type") == "agent"
        ]
        if candidates:
            slugged = {h for _, h in self.store.list_slugs()}
            candidates.sort(
                key=lambda f: (f.get("created_at") or "", f.get("content_hash") or ""),
                reverse=True,
            )
            for f in candidates:
                if f["content_hash"] in slugged:
                    return f["content_hash"]
            return candidates[0]["content_hash"]
        raise UnknownAgent(ref)

    def transition_agent(
        self,
        ref: str,
        to_status: str,
        by: Optional[str] = None,
        created_at: Any = None,
    ) -> str:
        """Move an agent to ``to_status``, admitting only legal FSM edges.

        The new lifecycle guard: ``orienting → active → retiring → retired`` are
        the only moves; anything else — skipping a state, going backwards, or
        re-entering the current state (running ``ready`` twice) — raises
        :class:`IllegalAgentTransition`. The resolve-of-agent, read-of-current, and
        write-of-next are one cross-process read-modify-write (design §5), so ALL
        THREE run inside one ``transaction()`` (``BEGIN IMMEDIATE``). Resolving the
        agent INSIDE the lock closes a TOCTOU: a concurrent same-name re-ignite can
        rebind the slug to a new incarnation, and resolving before the lock would
        write this status thread to the superseded folio while the slug points at
        the new one. Under the lock, resolution + status RMW see one consistent
        slug. Returns the agent folio hash.
        """
        with self.store.transaction():
            return self._transition_agent_locked(
                ref, to_status, by=by, created_at=created_at
            )

    def _transition_agent_locked(
        self,
        ref: str,
        to_status: str,
        by: Optional[str] = None,
        created_at: Any = None,
    ) -> str:
        """The lock-held core of :meth:`transition_agent`: resolve + guard + write.

        Assumes the caller ALREADY holds the store write lock (an open
        ``transaction()``), so the resolve-current-read and the status write
        compose into a LARGER atomic roster write rather than committing on their
        own. This is what lets ``complete`` fold its transition together with the
        chain yield and the summary post into one unit: if any of the three fails,
        the whole batch rolls back, the agent stays ``retiring``, and the command
        is cleanly retryable (finding A). :meth:`transition_agent` is the
        standalone wrapper that opens the transaction around this. Returns the
        agent folio hash (the live incarnation resolved under the lock).
        """
        if to_status not in AGENT_LIFECYCLE:
            raise ValueError(f"unknown lifecycle status {to_status!r}")
        folio_hash = self._resolve_agent(ref)
        current = self.store.latest_statuses([folio_hash]).get(
            folio_hash, _INITIAL_STATUS
        )
        if to_status not in _LEGAL_TRANSITIONS.get(current, ()):
            raise IllegalAgentTransition(current, to_status)
        self.store.save_thread(
            from_id=folio_hash,
            to_id=folio_hash,
            type="status",
            weaver=by,
            content=to_status,
            created_at=created_at if created_at is not None else _now_utc(),
        )
        return folio_hash

    def agent_ready(self, ref: str, by: Optional[str] = None) -> str:
        """``orienting → active``: the agent has oriented and is now working."""
        return self.transition_agent(ref, "active", by=by)

    def agent_torch(self, ref: str, by: Optional[str] = None) -> str:
        """``active → retiring``: the agent has begun retirement."""
        return self.transition_agent(ref, "retiring", by=by)

    def agent_complete(self, ref: str, by: Optional[str] = None) -> str:
        """``retiring → retired``: the agent has retired from the roster."""
        return self.transition_agent(ref, "retired", by=by)

    def list_agents(
        self, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Registered agents (one per name), optionally filtered by lifecycle status.

        Iterates the slug table for slugs pointing at ``type=agent`` folios — so
        the result is keyed on the NAME and shows the current incarnation (a
        re-ignite's ``succession`` rebind points the slug at the newest folio), the
        faithful analogue of legacy's one-row-per-agent roster. Each record is the
        agent folio dict plus ``status`` (its current lifecycle state), ``name``
        (the slug), and ``meta`` (the parsed content envelope).
        """
        out: List[Dict[str, Any]] = []
        for slug, content_hash in self.store.list_slugs():
            folio = self.store.get_folio(content_hash)
            if not folio or folio.get("type") != "agent":
                continue
            st = self.status_of(content_hash)
            if status is not None and st != status:
                continue
            rec = dict(folio)
            rec["status"] = st
            rec["name"] = slug
            rec["meta"] = parse_agent_meta(folio.get("content")) or {}
            out.append(rec)
        return out

    def agent_activity(
        self, status: Optional[str] = None, now: Any = None
    ) -> List[Dict[str, Any]]:
        """The roster activity feed: each agent enriched with its folio activity.

        A faithful port of the legacy QM activity view (routes.py:281,300-316).
        "Activity" is a QUERY over authored folios, not a heartbeat: for each
        agent, its work folios (those with ``created_by == agent-id``, excluding
        the agent folios themselves) give the last-activity time, working site,
        last folio type, and folio count. ``activity_status`` is:

        - ``torching`` — the agent's lifecycle is ``retiring`` or ``retired``;
        - ``active``   — it authored a folio within the last
          :data:`ACTIVE_WINDOW_MINUTES` minutes;
        - ``idle``     — otherwise (including a freshly-registered agent with no
          work yet, exactly as legacy, where no authored folios ⇒ idle).

        Sorted most-recently-active first. ``now`` is injectable for testing.
        """
        now_dt = _parse_dt(now) or _now_utc()
        window = timedelta(minutes=ACTIVE_WINDOW_MINUTES)
        feed: List[Dict[str, Any]] = []
        for agent in self.list_agents(status=status):
            agent_id = agent.get("created_by")
            authored = [
                f
                for f in (self.store.folios_by_created_by(agent_id) if agent_id else [])
                if f.get("type") not in _NON_WORK_FOLIO_TYPES
            ]
            authored.sort(
                key=lambda f: (f.get("created_at") or "", f.get("content_hash") or ""),
                reverse=True,
            )
            most_recent = authored[0] if authored else None
            last_activity = most_recent.get("created_at") if most_recent else None
            working_site = (
                self.store.folio_site_slug(most_recent["content_hash"])
                if most_recent
                else None
            )
            last_folio_type = most_recent.get("type") if most_recent else None
            lifecycle = agent["status"]
            activity_status = "idle"
            if lifecycle in ("retiring", "retired"):
                activity_status = "torching"
            elif last_activity is not None:
                last_dt = _parse_dt(last_activity)
                if last_dt is not None and (now_dt - last_dt) < window:
                    activity_status = "active"
            meta = agent.get("meta") or {}
            feed.append(
                {
                    "agent_id": agent_id,
                    "name": agent.get("name"),
                    "agent_type": meta.get("agent_type"),
                    "status": lifecycle,
                    "content_hash": agent["content_hash"],
                    "registered_at": agent.get("created_at"),
                    "last_activity": last_activity,
                    "activity_status": activity_status,
                    "working_site": working_site,
                    "folio_count": len(authored),
                    "last_folio_type": last_folio_type,
                }
            )
        feed.sort(
            key=lambda a: (a["last_activity"] or a["registered_at"] or ""),
            reverse=True,
        )
        return feed

    # --- chain yields (the mill hand-off; design D4) -------------------------
    #
    # mill sets SKEIN_CHAIN_ID on every chain task, so an agent's ``complete``
    # leaves a yield in the chain's sack for the next task. The sack is mutable
    # chain bookkeeping, not content-addressed, so it lives in the ``sacks``
    # sidecar (store) — this layer just generates the yield id and reads back.

    def store_chain_yield(
        self,
        chain_id: str,
        task_id: str,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        outcome: Optional[str] = None,
        artifacts: Optional[List[str]] = None,
        notes: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        tokens_used: Optional[int] = None,
        shard_path: Optional[str] = None,
        tender_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        sack_id: Optional[str] = None,
    ) -> str:
        """Store a yield into a chain's sack; return its ``sack_id``.

        ``sack_id`` defaults to a fresh ``sack-YYYYMMDD-xxxx`` id (legacy's
        :func:`skein.utils.generate_yield_id`, reused for byte-faithful ids).
        """
        from skein.utils import generate_yield_id

        sid = sack_id or generate_yield_id()
        self.store.add_yield(
            sack_id=sid,
            chain_id=chain_id,
            task_id=task_id,
            agent_id=agent_id,
            status=status,
            outcome=outcome,
            artifacts=artifacts,
            notes=notes,
            duration_seconds=duration_seconds,
            tokens_used=tokens_used,
            shard_path=shard_path,
            tender_id=tender_id,
            metadata=metadata,
        )
        return sid

    def chain_yields(self, chain_id: str) -> List[Dict[str, Any]]:
        """All yields in a chain, in execution order."""
        return self.store.get_chain_yields(chain_id)

    def chain_yield(self, sack_id: str) -> Optional[Dict[str, Any]]:
        """One yield by its sack id, or ``None``."""
        return self.store.get_yield(sack_id)

    def previous_chain_yield(
        self, chain_id: str, before_task_id: str
    ) -> Optional[Dict[str, Any]]:
        """The most recent yield in a chain before ``before_task_id`` (or ``None``)."""
        return self.store.get_previous_yield(chain_id, before_task_id)
