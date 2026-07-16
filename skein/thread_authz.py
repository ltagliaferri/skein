"""By-ends thread authorization for the station ingress (rev 6, brief-20260716-u73x).

The station admits any signed manifest leaf under a bound signer with NO per-edge
target authorization (the hole finding-20260710-lx37 pin #1 names). This module is
the enforcement core that closes it: every published thread is ``from_id -> to_id``,
typed, and authorized PER CLASS by two DISTINCT predicates —

  - ``owns_folio``          the FROM-end. PURE per-folio ownership: the signer is the
                            covering-manifest signer of the ``from_id`` folio ITSELF
                            (its own ``constituent_attribution``). NEVER satisfiable by
                            a grant; fail-closed on an unheld folio origin.
  - ``may_act_on_lineage``  the TO-end. Satisfiable by owner-of-genesis OR a station
                            tier (operator/administrator/steward) OR an active
                            per-document grant, evaluated at ``lineage_genesis_for(to_id)``.

A grant satisfies ONLY the to-end. The question is always "may this signer ORIGINATE
this thread, given its type and endpoints" — never "may they edit" (there is no edit
primitive; the only mechanism is supersession) and never "who authored" (there is no
authorship; a lineage's OWNER is the signer of its GENESIS version).

The module is pure over a ``store`` (a :class:`~skein.station_store.StationStore`, or
any object exposing the same read surface), the way :mod:`skein.authorization` is pure
over a ``BindingStore`` — so the classifier, the genesis resolver, and the per-class
rule unit-test with no server and no crypto. The ingress wires :func:`authorize_thread`
into its ``BEGIN IMMEDIATE`` transaction (skein/ingress.py), re-reading tier + grants
LIVE there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .authorization import Principal


class ThreadClass(Enum):
    """The authorization class of a thread type — how its two ends are gated.

    - AFFECTING       from-end ownership AND a to-end right (supersedes, within).
    - POINTER         from-end ownership, NO to-end right (reference/reply/…/tag).
    - CONTROL         to-end right ONLY; the from-end is STRUCTURAL, not an origin
                      (status self-loop, reverted head) — the from-end check is NOT
                      applied, else steward/administrator moderation wrong-blocks.
    - ASSIGNMENT      its own rule: supersession at ``lineage_genesis_for(from_id)``;
                      no from-end ownership (a steward may assign another's folio) and
                      no to-end resolve (the to-end is an agent).
    - ATTRIBUTION     from-end ownership ONLY (folio -> agent owner-only self-torch);
                      no to-end. Distinct from ASSIGNMENT despite the shared shape.
    - NON_FOLIO       from-end is not (necessarily) a folio: if ``from_id`` IS a held
                      folio apply from-end ownership; if it is a NON-folio (an agent
                      id) REJECT on the published graph (no station principal
                      authorizes an agent origin day-one).
    - WIRE_REJECT     does not travel the SIGNED published graph (a local-ledger
                      bookkeeping edge); rejected at ingress.

    POSTURE: every class here is enforced ONLY under ``require_signed`` (ON) — the
    ingress calls :func:`authorize_thread` only on that path. Under OFF the surface is
    authorization-blind BY DESIGN (byte-identical to the pre-mesh posture, rev 6 §8/§9:
    "OFF posture has NO edit authorization — single-party/local ONLY; a multi-party
    station MUST run ON"). So statements like "published never travels" and "archive is
    inert" are claims about the ON graph; an OFF station stores whatever hashes.
    """

    AFFECTING = "affecting"
    POINTER = "pointer"
    CONTROL = "control"
    ASSIGNMENT = "assignment"
    ATTRIBUTION = "attribution"
    NON_FOLIO = "non_folio"
    WIRE_REJECT = "wire_reject"


@dataclass(frozen=True)
class TypeSpec:
    """One thread type's authorization class + whether it confers station READ power.

    ``reads`` is the read-effect flag, co-located with the authorization class so the
    two cannot drift: a NEW thread type must declare BOTH (the drift test fails until
    it is added here), so a type can never silently gain read power without an
    authorization class. ``reads`` is False for a type that carries no station read
    power today (``tag``, ``archive``, off-graph/bookkeeping edges); the heads-only
    display + pointer-child guard (rev 6 §7) consume it.
    """

    klass: ThreadClass
    reads: bool


# The 17-literal taxonomy — the SINGLE source of truth for thread-type
# classification. Its key set MUST equal the union of the code's thread-type sets
# (models.ThreadType ∪ envelope._LINEAGE_THREADS/_STRUCTURAL_THREADS ∪
# storage.CONTROL_THREAD_TYPES/CLASS_B_GENESIS_DISPLAY_TYPES ∪
# threads_pk_swap.VERSION_ANCHORED_TYPES); a drift test asserts the equality in BOTH
# directions and fails if any of those sets grows an unclassified member (§8).
#
# Explicit placements that reconcile prior-rev contradictions (§5.1):
#   imports_legacy/forks/responds_to -> POINTER (+ the §7 pointer-child guard)
#   archive                          -> CONTROL, inert (nothing writes/reduces it)
#   published                        -> WIRE_REJECT (a folio->instance ledger edge)
#   tag                              -> POINTER, reads=False (no read power today —
#                                       audited: zero references in render/web/routes/
#                                       storage/resolve/mesh)
_TAXONOMY: Dict[str, TypeSpec] = {
    # AFFECTING — from-end ownership + to-end right
    "supersedes": TypeSpec(ThreadClass.AFFECTING, reads=True),
    "within": TypeSpec(ThreadClass.AFFECTING, reads=True),
    # POINTER — from-end ownership, no to-end right
    "reference": TypeSpec(ThreadClass.POINTER, reads=True),
    "reply": TypeSpec(ThreadClass.POINTER, reads=True),
    "mention": TypeSpec(ThreadClass.POINTER, reads=True),
    "forks": TypeSpec(ThreadClass.POINTER, reads=True),
    "responds_to": TypeSpec(ThreadClass.POINTER, reads=True),
    "imports_legacy": TypeSpec(ThreadClass.POINTER, reads=True),
    "tag": TypeSpec(ThreadClass.POINTER, reads=False),
    # CONTROL — to-end right only, from-end structural/exempt
    "status": TypeSpec(ThreadClass.CONTROL, reads=True),
    "reverted": TypeSpec(ThreadClass.CONTROL, reads=True),
    "archive": TypeSpec(ThreadClass.CONTROL, reads=False),
    # ASSIGNMENT — its own rule
    "assignment": TypeSpec(ThreadClass.ASSIGNMENT, reads=True),
    # ATTRIBUTION — from-end ownership only (folio -> agent self-torch)
    "attribution": TypeSpec(ThreadClass.ATTRIBUTION, reads=False),
    # NON_FOLIO — reject an agent origin on the published graph
    "message": TypeSpec(ThreadClass.NON_FOLIO, reads=False),
    "succession": TypeSpec(ThreadClass.NON_FOLIO, reads=False),
    # WIRE_REJECT — never travels
    "published": TypeSpec(ThreadClass.WIRE_REJECT, reads=False),
}


class UnknownThreadType(ValueError):
    """A thread type absent from :data:`_TAXONOMY`. The classifier is exhaustive and
    fail-closed: an unknown type is a REJECT, never a silent default (§5.1)."""


def classify_thread(thread_type: str) -> ThreadClass:
    """The authorization class of ``thread_type``, or raise :class:`UnknownThreadType`.

    Fail-closed (§5.1/§8): a type not in the taxonomy raises rather than defaulting to
    any permissive class, so a thread type added to the code without an authorization
    class is REJECTED at ingress (and the drift test fails in CI first)."""
    spec = _TAXONOMY.get(thread_type)
    if spec is None:
        raise UnknownThreadType(thread_type)
    return spec.klass


def thread_reads(thread_type: str) -> bool:
    """Whether ``thread_type`` confers station read power (the read-effect map).

    Fail-closed like :func:`classify_thread`: an unknown type raises rather than
    being assumed inert."""
    spec = _TAXONOMY.get(thread_type)
    if spec is None:
        raise UnknownThreadType(thread_type)
    return spec.reads


def known_thread_types() -> frozenset:
    """The taxonomy's key set — the classified thread-type universe (for the drift test)."""
    return frozenset(_TAXONOMY)


# --- genesis resolution -----------------------------------------------------

# The tiers that may act on ANY lineage's to-end (moderate/supersede/contribute)
# without owning its genesis or holding a per-document grant. operator is included
# implicitly (the root tier); administrator + steward are the day-one delegated
# tiers. contributor/curator/reader are reserved and unbuilt — deliberately ABSENT
# so an unbuilt tier never satisfies a check.
_TIER_MAY_ACT = frozenset({"operator", "administrator", "steward"})

# Which stored grant KINDS satisfy a requested to-end right. A site_edit grant is the
# broader site power, so it ALSO satisfies a site_contribute request; supersede is
# exact. The store's has_active_grant is an exact-kind mechanism — this implication is
# the policy layer, kept here beside the predicate that consumes it.
_SATISFYING_GRANTS = {
    "supersede": ("supersede",),
    "site_contribute": ("site_contribute", "site_edit"),
    "site_edit": ("site_edit",),
}


@dataclass(frozen=True)
class Genesis:
    """The result of walking a lineage back to its anchor.

    - ``hash`` — the anchor reached.
    - ``resolved`` — True when the walk reached a HELD parentless anchor (a clean
      lineage). False on a LEGACY GAP: the chain's oldest held node still carries a
      ``supersedes`` edge to an UNHELD parent, but that child is UNSIGNED (no covering
      manifest), so the gap is legacy incompleteness, not forgery. On a gap the owner
      is unresolvable and grants cannot attach, so ONLY a station tier
      (steward+/administrator/operator) may act — the legacy-moderation path (§4).
    """

    hash: str
    resolved: bool


class LineageReject(ValueError):
    """The genesis resolver failed CLOSED: a merge (>1 supersedes parent), a cycle, a
    self-edge, or a SIGNED-chain gap (a signed child superseding an unheld parent — a
    forgery signal). Distinct from a legacy gap, which degrades rather than rejects."""


def _pending_pairs(
    pending_supersedes: Iterable[Any],
) -> List[Tuple[Optional[str], Optional[str]]]:
    """Normalize a pending-supersedes iterable (thread dicts or ``(from, to)`` pairs)
    to a list of ``(from_id, to_id)`` tuples."""
    out: List[Tuple[Optional[str], Optional[str]]] = []
    for e in pending_supersedes:
        if isinstance(e, dict):
            out.append((e.get("from_id"), e.get("to_id")))
        else:
            out.append((e[0], e[1]))
    return out


def _supersedes_parents(
    store, node: str, pending: Sequence[Tuple[Optional[str], Optional[str]]]
) -> List[str]:
    """The DISTINCT ``supersedes`` parents of ``node`` — the ``to_id`` of every
    ``supersedes`` edge whose ``from_id`` is ``node`` (stored edges the station holds
    PLUS the authorized same-batch ``pending`` edges). ≤1 in a well-formed graph; >1 is
    a MERGE the caller fails closed on. Deduped, insertion-order preserved."""
    parents: List[str] = [
        e["to_id"] for e in store.get_threads(from_id=node, type="supersedes")
    ]
    parents += [t for (f, t) in pending if f == node]
    out: List[str] = []
    seen = set()
    for p in parents:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def lineage_genesis_for(
    store, version: str, pending_supersedes: Iterable[Any] = ()
) -> Genesis:
    """Walk ``supersedes`` BACKWARD from ``version`` to the single parentless genesis.

    A ``supersedes`` edge is ``(from_id=new, to_id=old)``, so a version's PARENT (its
    older predecessor) is the ``to_id`` of a ``supersedes`` whose ``from_id`` is that
    version. The walk follows parents to the anchor with no parent.

    ``pending_supersedes`` is the staged same-batch supersedes set — an iterable of
    thread dicts or ``(from_id, to_id)`` pairs — merged with the stored edges so a
    same-batch chain resolves to its true genesis. It MUST be the AUTHORIZED subset
    (the ingress builds it as a fixpoint, §8): a rejected same-batch supersedes must
    never steer another edge's genesis.

    Fails CLOSED (raises :class:`LineageReject`) on: >1 parent (a MERGE — the only
    thing that makes genesis multi-valued), a cycle, a self-edge, or a SIGNED-chain
    gap. A LEGACY gap (an unsigned child superseding an unheld parent) returns
    ``Genesis(resolved=False)`` so the steward+ moderation path stays reachable (§4).
    The seen-set is seeded with ``version`` so a 2-cycle at the start is caught."""
    pending = _pending_pairs(pending_supersedes)

    seen = {version}
    node = version
    while True:
        parents = _supersedes_parents(store, node, pending)
        if not parents:
            return Genesis(node, resolved=True)
        if len(parents) > 1:
            raise LineageReject(
                f"merge: {node!r} has {len(parents)} supersedes parents (>1 makes "
                "genesis multi-valued)"
            )
        parent = parents[0]
        if parent == node:
            raise LineageReject(f"self-edge supersedes at {node!r}")
        if parent in seen:
            raise LineageReject(f"cycle in supersedes lineage at {parent!r}")
        if store.get_folio(parent) is None:
            # A dangling parent: the chain references a predecessor not held here. On a
            # SIGNED child this is a forgery signal (a signed chain never legitimately
            # references an unadmitted parent — the migration repairs pre-existing
            # dangling rows, §4) -> reject. On an UNSIGNED/legacy child it is import
            # incompleteness -> degrade to an unresolved genesis (owner=None; steward+
            # only).
            if store.get_constituent_proof(node) is not None:
                raise LineageReject(
                    f"signed-chain gap: parent {parent!r} of signed {node!r} is not held"
                )
            return Genesis(node, resolved=False)
        seen.add(parent)
        node = parent


def reachable_ancestors(store, node: str, pending_supersedes: Iterable[Any] = ()) -> set:
    """The set of strict ancestors of ``node`` reachable by walking ``supersedes``
    BACKWARD (older predecessors), over stored + staged edges. Fails CLOSED
    (:class:`LineageReject`) on a merge/cycle/self-edge, exactly like
    :func:`lineage_genesis_for`. Used by ``reverted`` to require that its target is a
    reachable ANCESTOR of the head, not merely a same-genesis sibling (§5.4)."""
    pending = _pending_pairs(pending_supersedes)
    seen = {node}
    ancestors: set = set()
    cur = node
    while True:
        parents = _supersedes_parents(store, cur, pending)
        if not parents:
            return ancestors
        if len(parents) > 1:
            raise LineageReject(f"merge: {cur!r} has {len(parents)} supersedes parents")
        parent = parents[0]
        if parent == cur:
            raise LineageReject(f"self-edge supersedes at {cur!r}")
        if parent in seen:
            raise LineageReject(f"cycle in supersedes lineage at {parent!r}")
        ancestors.add(parent)
        seen.add(parent)
        cur = parent


# --- the two authorization predicates ---------------------------------------


def _proof_signer(store, content_hash: str) -> Optional[Tuple[str, str]]:
    """The (issuer, subject) of a constituent's OWN covering-manifest attribution, or
    None when it has none (unheld/unsigned) OR its proof is incomplete.

    A ``proof_missing`` row (attribution present but its manifest parent gone —
    corruption / mid-migration / a non-ingress writer) yields NO owner: the READ path
    already refuses to call such a folio verified ('NOT VERIFIED — proof missing',
    envelope.folio_verdict step 1/ST5), so trusting its DENORMALIZED (issuer, subject)
    on the WRITE path would let the named signer act as owner over a folio the station
    will not vouch for — a fail-open the read side rejects. Fail closed: no proof, no
    owner (only a station tier can act on such a lineage)."""
    proof = store.get_constituent_proof(content_hash)
    if proof is None or proof.get("proof_missing"):
        return None
    return (proof["issuer"], proof["subject"])


def owns_folio(store, signer: Principal, folio_hash: str) -> bool:
    """FROM-end ownership (PURE — NEVER grant-satisfiable): ``signer`` is the covering-
    manifest signer of the ``folio_hash`` FOLIO ITSELF (its own attribution), AND that
    hash is a HELD folio (present in ``versions``, not a thread hash or an unheld id).
    An unheld or unsigned folio origin FAILS CLOSED (§5.1/§5.6 anti-forgery,
    anti-pre-plant). The held-folio check keeps a thread_hash the signer happens to have
    published from satisfying a folio from-end (attribution is keyed by hash regardless
    of kind — fell-r2)."""
    if store.get_folio(folio_hash) is None:
        return False
    owner = _proof_signer(store, folio_hash)
    return owner is not None and owner == (signer.issuer, signer.subject)


def lineage_owner(store, genesis: str) -> Optional[Tuple[str, str]]:
    """The owner of a lineage: the signer of its GENESIS version, or None (pre-ON /
    legacy, no covering manifest). None means NO owner right — never a None==None
    match against an unbound signer (§4)."""
    return _proof_signer(store, genesis)


def may_act_on_lineage(
    store, signer: Principal, genesis: Genesis, kind: str
) -> bool:
    """TO-end right at a lineage genesis: owner-of-genesis | station tier | active
    per-document grant of ``kind`` at the genesis anchor. A grant satisfies ONLY this
    predicate, never the from-end (§5.1/§5.2).

    On an UNRESOLVED genesis (a legacy gap) owner and grant are BOTH skipped — the
    anchor is indeterminate, so only a station tier may act (§4)."""
    if genesis.resolved:
        owner = lineage_owner(store, genesis.hash)
        if owner is not None and owner == (signer.issuer, signer.subject):
            return True
    binding = store.get_binding(signer.issuer, signer.subject)
    if binding is not None and binding.revoked_at is None and binding.role in _TIER_MAY_ACT:
        return True
    if genesis.resolved:
        for gk in _SATISFYING_GRANTS.get(kind, (kind,)):
            if store.has_active_grant(
                genesis.hash, signer.issuer, signer.subject, gk
            ):
                return True
    return False


# --- the per-class rule (the enforcement core) ------------------------------

# A content-address endpoint: bare 64-hex or the framed sha256::<hex>. An endpoint
# that is supposed to name a FOLIO but is not a content address is a legacy ALIAS
# (or a slug) and is REJECTED on the signed path — aliases are read-side/migration
# only, and canonicalizing at ingress would break the signed thread_hash (§5.1).
_CONTENT_ADDRESS_RE = re.compile(r"^(?:sha256::)?[0-9a-f]{64}$")


def _is_content_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_CONTENT_ADDRESS_RE.match(value))


# Reject-ack strings (§8). NOT a versioned/federated canon surface (audited: the ack
# rides plain under protocol skein-publish/v0, no client branches on the text) — human
# diagnostics, kept stable and free of any secret (the lineage/site hashes they name
# are already public on the read surface).
_FROM_OWN_REJECT = "cannot originate from a folio you do not hold"


class AuthzReject(ValueError):
    """A thread is not authorized. ``reason`` is the wire reject-ack string (§8)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _require_content_hash(endpoint: Any, role: str) -> None:
    if not _is_content_hash(endpoint):
        raise AuthzReject(f"{role} must be a content hash, not an alias or slug")


def _resolve_or_reject(store, node: str, pending) -> Genesis:
    """lineage_genesis_for, converting a fail-closed :class:`LineageReject` into an
    :class:`AuthzReject` with a stable wire reason (the ingress rejects either way)."""
    try:
        return lineage_genesis_for(store, node, pending)
    except LineageReject as e:
        raise AuthzReject(
            f"unresolvable target lineage for {node} (merge/cycle/dangling)"
        ) from e


def authorize_thread(
    store,
    signer: Principal,
    thread: Mapping[str, Any],
    pending_supersedes: Iterable[Any] = (),
) -> None:
    """Authorize one published thread ``from_id -> to_id`` for ``signer``, or raise
    :class:`AuthzReject`. The enforcement core (§5.1), wired into the ingress inside its
    BEGIN IMMEDIATE transaction. ``signer`` is the verified, bound manifest signer;
    ``pending_supersedes`` is the AUTHORIZED same-batch supersedes staging set (§8).

    Two DISTINCT predicates gate the two ends: ``owns_folio`` (from-end, PURE
    ownership, never grant-satisfiable) and ``may_act_on_lineage`` (to-end, tier/grant).
    Fail-closed throughout: an unknown type, an alias endpoint, an unheld origin/target,
    or an unresolvable lineage all reject."""
    ttype = thread.get("type")
    frm = thread.get("from_id")
    to = thread.get("to_id")

    try:
        klass = classify_thread(ttype)
    except UnknownThreadType:
        raise AuthzReject(f"unknown thread type {ttype!r}") from None

    if klass is ThreadClass.WIRE_REJECT:
        raise AuthzReject(
            f"{ttype} is a local-ledger edge and does not travel the published graph"
        )

    if klass is ThreadClass.AFFECTING:
        _require_content_hash(frm, "from_id")
        _require_content_hash(to, "to_id")
        # FROM-end: PURE ownership of the held origin folio (fail-closed on unheld).
        if not owns_folio(store, signer, frm):
            raise AuthzReject(_FROM_OWN_REJECT)
        # TO-end: fail-closed on a dangling target (§5.6), then the to-end right.
        if store.get_folio(to) is None:
            raise AuthzReject(f"affecting edge target {to} is not held")
        if ttype == "within":
            # within.to_id MUST be the site GENESIS (reject a head-anchored within, so
            # auth/folio_site_slug/folios_in_site all key on the genesis, §5.2/§5.5).
            genesis = _resolve_or_reject(store, to, pending_supersedes)
            if not genesis.resolved or genesis.hash != to:
                raise AuthzReject(
                    f"within must anchor at the site genesis, not the head {to}"
                )
            if not may_act_on_lineage(store, signer, genesis, "site_contribute"):
                raise AuthzReject(f"no contribute access to {genesis.hash}")
        else:  # supersedes
            # <=1-parent admission (§5.3): the NEW version (from_id) must not already
            # have a supersedes parent to a DIFFERENT predecessor. A parent to the SAME
            # to_id is an idempotent re-publish (allowed); a parent to a different one is
            # a MERGE (rejected here, and by the partial-unique index as insurance). A
            # self-edge (from_id == to_id) is rejected outright.
            if frm == to:
                raise AuthzReject("supersedes self-edge (from_id == to_id)")
            other_parents = {
                e["to_id"] for e in store.get_threads(from_id=frm, type="supersedes")
            }
            other_parents |= {t for (f, t) in _pending_pairs(pending_supersedes) if f == frm}
            other_parents.discard(to)
            if other_parents:
                raise AuthzReject(
                    "supersedes would give the new version a second parent (merge)"
                )
            # Resolve the to-end genesis with THIS edge included in the pending set, so a
            # supersedes that would CLOSE A CYCLE (e.g. co-batch A->B plus B->A) is
            # detected and rejected here — before it is staged or stored, where it would
            # poison a co-batch edge's genesis resolution (a cycle confused-deputy).
            genesis = _resolve_or_reject(
                store, to, list(_pending_pairs(pending_supersedes)) + [(frm, to)]
            )
            if not may_act_on_lineage(store, signer, genesis, "supersede"):
                raise AuthzReject(f"no edit access to {genesis.hash}; fork instead")
        return

    if klass is ThreadClass.POINTER:
        # FROM-end ownership of a held folio origin; NO to-end RIGHT (a pointer carries
        # no power on its target). Both endpoints must still be content hashes — an ALIAS
        # to-end is REJECTED (§5.1: no wire path writes aliases; else a signed pointer
        # would resolve through the mutable aliases table to a target its signature never
        # fixed). The to-end may be DANGLING (unheld) — only alias/slug shapes reject.
        _require_content_hash(frm, "from_id")
        _require_content_hash(to, "to_id")
        if not owns_folio(store, signer, frm):
            raise AuthzReject(_FROM_OWN_REJECT)
        return

    if klass is ThreadClass.CONTROL:
        # from-end OWNERSHIP is exempt (structural, not an origin) — else steward/
        # administrator moderation wrong-blocks. But the structural SHAPE is still
        # enforced (an agent-origin/alias from_id must never reach the published graph,
        # §5.1/§9): status is a self-loop, reverted/archive carry content-hash endpoints.
        # Gated on the to-end right at the controlled genesis.
        if ttype == "reverted":
            _require_content_hash(frm, "from_id")
            _require_content_hash(to, "to_id")
            if store.get_folio(frm) is None:
                raise AuthzReject(f"reverted head {frm} is not held")
            if store.get_folio(to) is None:
                raise AuthzReject(f"reverted target {to} is not held")
            # from_id must be the CURRENT HEAD — nothing supersedes it (§5.4). A revert
            # runs from the head back to an ancestor; a revert from an already-superseded
            # version is malformed.
            if store.get_threads(to_id=frm, type="supersedes") or any(
                t == frm for (f, t) in _pending_pairs(pending_supersedes)
            ):
                raise AuthzReject(
                    f"reverted from_id {frm} is not the current head (it is superseded)"
                )
            # to_id must be a reachable ANCESTOR of the head (from_id), not merely a
            # same-genesis sibling in a fork (§5.4).
            try:
                ancestors = reachable_ancestors(store, frm, pending_supersedes)
            except LineageReject as e:
                raise AuthzReject(
                    f"unresolvable head lineage for reverted {frm} (merge/cycle)"
                ) from e
            if to not in ancestors:
                raise AuthzReject(
                    f"reverted target {to} is not a reachable ancestor of the head {frm}"
                )
            genesis = _resolve_or_reject(store, frm, pending_supersedes)
        else:  # status, archive — a self-loop ANCHORED AT THE GENESIS (§5.4)
            # Both are from=to=genesis self-loops (the workbench _genesis_key_control
            # writes them so, and verify_threads_control flags a non-self-loop as
            # invariant I2). Enforce the FULL shape: from==to (rejects any agent-origin
            # from_id) AND the self-loop is on the lineage GENESIS (rejects a self-loop
            # on a non-genesis head), so control state stays single-valued per lineage.
            if frm != to:
                raise AuthzReject(f"{ttype} must be a self-loop (from_id == to_id)")
            _require_content_hash(to, "to_id")
            if store.get_folio(to) is None:
                raise AuthzReject(f"control target {to} is not held")
            genesis = _resolve_or_reject(store, to, pending_supersedes)
            if genesis.hash != to:
                raise AuthzReject(
                    f"{ttype} must anchor at the lineage genesis, not the head {to}"
                )
        if not may_act_on_lineage(store, signer, genesis, "supersede"):
            raise AuthzReject(f"no moderation access to {genesis.hash}")
        return

    if klass is ThreadClass.ASSIGNMENT:
        # from = folio GENESIS, to = assignee AGENT. Gate on supersession at
        # lineage_genesis_for(from_id): no from-end ownership (a steward may assign
        # another's folio), no to-end resolve (the to-end is an agent, §5.4).
        _require_content_hash(frm, "from_id")
        if store.get_folio(frm) is None:
            raise AuthzReject(f"assignment source folio {frm} is not held")
        genesis = _resolve_or_reject(store, frm, pending_supersedes)
        # from_id is the folio GENESIS (§5.1), not a head — so a genesis-keyed assignment
        # reducer keys on the anchor the design mandates.
        if genesis.hash != frm:
            raise AuthzReject(
                f"assignment from_id must be the lineage genesis, not the head {frm}"
            )
        if not may_act_on_lineage(store, signer, genesis, "supersede"):
            raise AuthzReject(f"no assignment access to {genesis.hash}")
        return

    if klass is ThreadClass.ATTRIBUTION:
        # folio -> agent owner-only self-torch: FROM-end ownership of the folio; no
        # to-end (the to-end is an agent). Distinct from ASSIGNMENT (§5.1).
        _require_content_hash(frm, "from_id")
        if not owns_folio(store, signer, frm):
            raise AuthzReject(_FROM_OWN_REJECT)
        return

    if klass is ThreadClass.NON_FOLIO:
        # message / succession: if from_id IS a held folio, apply FROM-end ownership;
        # if it is a NON-folio (an agent id / alias), REJECT on the published graph —
        # no station principal authorizes an agent origin day-one (§5.1, §9).
        if _is_content_hash(frm) and store.get_folio(frm) is not None:
            if not owns_folio(store, signer, frm):
                raise AuthzReject(_FROM_OWN_REJECT)
            return
        raise AuthzReject(
            "no station principal authorizes an agent-origin thread on the published graph"
        )

    # Defensive: classify_thread is exhaustive, so this is unreachable — but never fall
    # through to an implicit accept.
    raise AuthzReject(f"unhandled thread class {klass!r} for {ttype!r}")


# --- the staged final graph (same-batch supersedes) -------------------------


def authorized_staged_supersedes(
    store, signer: Principal, batch_supersedes: Sequence[Mapping[str, Any]]
) -> List[Tuple[str, str]]:
    """The FIXPOINT of same-batch ``supersedes`` edges that PASS authorization — the
    staging graph an affecting edge's genesis is resolved over (§8).

    A batch may carry both ``within(member -> S1)`` and ``supersedes(S1 -> S0)``, so
    genesis must resolve over the batch's pending supersedes, not incrementally. But the
    staging set MUST be the AUTHORIZED subset only (knuth F2): a rejected same-batch
    supersedes must never steer another edge's genesis, or it becomes a confused deputy.
    So build the set from edges that authorize, and iterate — an edge's authorization can
    depend on another same-batch edge already being staged (a chain deepens genesis) — to
    a fixpoint. A genuinely unauthorizable edge simply never enters the set (and is
    rejected on its own in the main pass).
    """
    remaining = list(batch_supersedes)
    staged: List[Tuple[str, str]] = []
    changed = True
    while changed and remaining:
        changed = False
        still: List[Mapping[str, Any]] = []
        for edge in remaining:
            try:
                authorize_thread(store, signer, edge, staged)
            except (AuthzReject, LineageReject):
                still.append(edge)  # may authorize once more edges stage
                continue
            staged.append((edge["from_id"], edge["to_id"]))
            changed = True
        remaining = still
    return staged
