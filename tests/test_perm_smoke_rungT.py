"""Workstream B — the adversarial smoke at RUNG T (brief-20260716-93t3 §4, §7).

Rung T is *two threads, two connections, one DB file*: it adds **write-lock contention**
and **read-placement probes** over rung 2's single-connection ``ingest()``. Kept in its
own file because every test here needs a db PATH and several ``Station`` connections over
it, not the single shared ``Station`` fixture the rung-2 smoke (test_perm_smoke.py) uses.

Two scenarios:

**S9 — win a race.** Two publishes race the same ``from_id`` against the ≤1-parent rule.
Rev 6 §5.3's argument for that rule is *entirely a placement argument*: the check sits
inside ``BEGIN IMMEDIATE``, so concurrent writers serialize on the write lock and the
loser sees the winner's committed parent. §6's placement-mutant catalogue names exactly
this — "the ≤1-parent check's position inside ``BEGIN IMMEDIATE``, guarding the
load-bearing invariant". The test pins WHICH guard refused, so a check that drifted
outside the lock and got rescued by the partial-unique index fails here instead of
passing on the shared "(merge)" tail.

**S5 — outlive a revocation.** The plan's headline PLACEMENT MUTANT (§6): move the
ingress's live binding read from inside ``BEGIN IMMEDIATE`` to before it — the
"pre-filter" architecture rev 6 §8 itself warns about — and **all 130 existing tests stay
green**. Two reviewers built that mutant independently and nothing in the repo killed it.
The property has no other guard, so these tests are its only guard. Both S5 tests fail
against the hoisted read; see the module note on S5 below for the two mechanisms.

Style follows test_perm_smoke.py: attacker goals, the SPECIFIC reject reason (a
right-answer-wrong-reason refusal is a bug wearing a pass), and a negative/positive
control wherever one guard is claimed load-bearing.
"""

from __future__ import annotations

import concurrent.futures as cf
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

import pytest

from skein.identity import compute_thread_hash
from skein.station import Station
from skein.thread_authz import lineage_genesis_for
from tests import perm_multiparty as mp
from tests import station_publish_helpers as h
from tests.station_publish_helpers import I

_TS = "2026-01-01T00:00:00+00:00"
BOB_TS = "2026-06-01T00:00:00+00:00"

# The two "(merge)" refusals the ≤1-parent invariant can produce. They are DISTINCT on
# purpose (fell r1): the authz check inside BEGIN IMMEDIATE says "would GIVE", the
# partial-unique index backstop on threads(from_id) says "would CREATE". S9 pins which.
_AUTHZ_MERGE = "supersedes would give the new version a second parent (merge)"
_INDEX_MERGE = "supersedes would create a second parent (merge)"


def _materialize(root: Path, name: str = "instance") -> Path:
    """Birth a station db under ``root/name`` and return its PATH."""
    p = root / name / ".skein-next"
    Station(p).close()
    return p


@pytest.fixture
def db(tmp_path) -> Path:
    """A materialized station db PATH. Rung T opens a Station PER CONNECTION over it."""
    return _materialize(tmp_path)


@contextmanager
def _station(path: Path):
    st = Station(path, check_same_thread=False)
    try:
        yield st
    finally:
        st.close()


def _thread(ttype: str, frm: str, to: str, created_at: str = BOB_TS) -> Dict[str, Any]:
    th = compute_thread_hash(
        from_id=frm, to_id=to, type=ttype, weaver=None, created_at=created_at, content=None,
    )
    return {
        "thread_hash": th, "from_id": frm, "to_id": to, "type": ttype,
        "weaver": None, "created_at": created_at, "content": None,
    }


def _reject_reason(ack, thread_hash) -> str:
    rej = [r for r in ack["threads"]["rejected"] if r["thread_hash"] == thread_hash]
    return rej[0]["reason"] if rej else "<accepted or absent>"


def _attributions_to(store, subject: str):
    """Every ``constituent_attribution`` row naming ``subject`` — the ownership record a
    write leaves behind. A revoked signer must leave NONE."""
    return [
        (r["constituent_hash"], r["kind"])
        for r in store.conn.execute(
            "SELECT constituent_hash, kind FROM constituent_attribution WHERE subject = ?",
            (subject,),
        )
    ]


# ============================================================================
# S9 — WIN A RACE (§7 S9, rung T). Two publishes race the same from_id against the
# ≤1-parent rule: BOB (bound, and the owner of every folio involved) publishes X->A on
# one connection and X->B on the other, concurrently, under ON. Both are individually
# legitimate; together they are a MERGE, which bricks a lineage — the genesis resolver
# fails closed on >1 parent BEFORE any tier check, so not even the operator can repair it.
#
# The invariant: EXACTLY ONE parent lands. Not zero (that would deny BOB a legitimate
# version — claim 2), not two (that would brick X's lineage — claim 1/2), and no thread
# may 500 (§2's "third outcome", which admits nothing but is a claim-2 falsifier on a
# legitimate publish).
# ============================================================================

def _bob_owns_three(store_path):
    """BOB (bound originator) publishes A, B (two genesis folios) and X (his own new
    version). Returns ``(x, a, b)`` content hashes. All three are BOB's, so both racing
    edges satisfy the from-end (owns_folio X) and the to-end (owner-of-genesis) — the
    ONLY thing that can refuse either is the ≤1-parent rule."""
    with _station(store_path) as st:
        mp.bind_party(st, mp.BOB, "originator")
        a = h.folio("finding", "A genesis", "a", _TS)
        b = h.folio("finding", "B genesis", "b", "2026-01-02T00:00:00+00:00")
        x = h.folio("finding", "X new version", "x", BOB_TS)
        ack = mp.publish_as(st, mp.BOB, [a, b, x], [])
        assert not ack["rejected"], ack["rejected"]
        return x["content_hash"], a["content_hash"], b["content_hash"]


def test_s9_racing_parents_exactly_one_lands(db):
    """Two connections, two threads, one db: X->A and X->B race. Exactly one lands, the
    other is refused BY THE AUTHZ CHECK (not the index backstop), neither thread raises,
    and X's lineage still resolves to a single genesis."""
    x, a, b = _bob_owns_three(db)
    e_a = _thread("supersedes", x, a)
    e_b = _thread("supersedes", x, b)

    start = threading.Barrier(2)

    def racer(edge):
        with _station(db) as st:
            start.wait(timeout=10)  # both threads enter ingest together
            return mp.publish_as(st, mp.BOB, [], [edge])

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(racer, e) for e in (e_a, e_b)]
        # .result() RE-RAISES: an uncaught exception in either racer fails here. That is
        # the "never a 500" observable (§2's third outcome) under real lock contention.
        acks = [f.result(timeout=60) for f in futures]

    accepted = [t for ack in acks for t in ack["threads"]["accepted"]]
    rejected = [r for ack in acks for r in ack["threads"]["rejected"]]
    both = {e_a["thread_hash"], e_b["thread_hash"]}

    assert len(accepted) == 1, (accepted, rejected)
    assert len(rejected) == 1, (accepted, rejected)
    assert set(accepted) | {r["thread_hash"] for r in rejected} == both
    # THE PLACEMENT ASSERTION (§6): with the check inside BEGIN IMMEDIATE the loser's
    # transaction begins only after the winner COMMITS, so the loser's authz check reads
    # the stored parent and refuses with "would GIVE". Reading the index backstop's
    # "would CREATE" here would mean the ≤1-parent check ran on a snapshot taken outside
    # the write lock and the DB caught what authz missed — the mutant §6 names.
    assert rejected[0]["reason"] == _AUTHZ_MERGE, rejected

    # Claim 1/2: one parent stored, lineage resolves, and the genesis is the winner's.
    with _station(db) as st:
        parents = st.store.get_threads(from_id=x, type="supersedes")
        assert len(parents) == 1, parents
        winner_to = parents[0]["to_id"]
        assert winner_to in (a, b)
        genesis = lineage_genesis_for(st.store, x)
        assert genesis.resolved and genesis.hash == winner_to
        # The loser's edge was never stored — the ack did not claim a row it did not
        # persist, and the refused parent left no trace.
        loser_hash = (both - set(accepted)).pop()
        assert st.store.get_thread(loser_hash) is None


def test_s9_control_each_racing_edge_is_admissible_alone(tmp_path):
    """POSITIVE CONTROL for S9: the loser lost the RACE, not a shape/rights check. Each
    edge, published ALONE on its own fresh instance, is accepted — so the single refusal
    above is attributable to the ≤1-parent rule and nothing else."""
    for which in (0, 1):
        path = _materialize(tmp_path, f"solo{which}")
        x, a, b = _bob_owns_three(path)
        edge = _thread("supersedes", x, (a, b)[which])
        with _station(path) as st:
            ack = mp.publish_as(st, mp.BOB, [], [edge])
            assert edge["thread_hash"] in ack["threads"]["accepted"], \
                _reject_reason(ack, edge["thread_hash"])


# ============================================================================
# S5 — OUTLIVE A REVOCATION (§7 S5, rung T). The read-placement property.
#
# The attack: BOB is bound at a TIER (steward) and publishes a tiered supersede onto
# ALICE's lineage — legitimate for a LIVE steward (test_perm_smoke.py S4 proves it lands),
# forbidden for a REVOKED one. An administrator revokes BOB while the publish is in
# flight. The revocation commits BEFORE this ingest takes the write lock, so it must flip
# THIS ingest's verdict, not the next one (rev 6 §8 / C40).
#
# The mutant this kills: hoist `live = bindings.get_binding(...)` above
# `with station.store.transaction():`. That is the pre-filter architecture §8 warns about,
# and it SURVIVES all 130 pre-existing tests — the existing concurrency tests drive 30
# threaded folio writers under OFF and never touch the binding read at all.
#
# Why the seam is `transaction()`: everything before it is identical under both
# placements, so the ONLY window that separates a hoisted read from a live one is the
# acquisition of BEGIN IMMEDIATE. A revocation that commits inside that window is read by
# the live code and missed by the hoisted code. Two tests construct that window two ways:
#
#   - _revoke_at_the_write_lock: injects the revocation exactly at the seam. Fully
#     deterministic, no sleeps.
#   - the contention test below: no injection at all — a second connection HOLDS the write
#     lock while it revokes, so the ingest genuinely blocks in BEGIN IMMEDIATE on
#     busy_timeout and reads the revocation only after winning the lock. Slower and uses
#     one bounded sleep, but nothing about the ingress is patched.
#
# `bindings` is injected in both to OBSERVE the read (ordering), never to fake it. Note
# `may_act_on_lineage` ALSO re-reads the binding per to-end authorization (1 + N for an
# N-supersedes batch) — it calls store.get_binding directly, not through this seam — so
# the recorder sees the INGRESS read only, which is the one under test.
# ============================================================================

def _victim_lineage(st):
    """ALICE (bound originator) owns genesis G and head V (V supersedes G). Returns
    ``(g, v)``. The foreign lineage a steward may moderate and a revoked one may not."""
    mp.bind_party(st, mp.ALICE, "originator")
    g = h.folio("finding", "alice genesis", "g", _TS)
    v = h.folio("finding", "alice head", "v", "2026-01-02T00:00:00+00:00")
    a1 = mp.publish_as(st, mp.ALICE, [g], [])
    a2 = mp.publish_as(st, mp.ALICE, [v],
                       [_thread("supersedes", v["content_hash"], g["content_hash"],
                                "2026-01-02T00:00:00+00:00")])
    assert not a1["rejected"] and not a2["threads"]["rejected"], (a1, a2)
    return g["content_hash"], v["content_hash"]


class _RecordingBindings:
    """A BindingStore wrapper that RECORDS the ingress's binding read into a shared
    event log, then delegates. It changes no verdict — it only makes the read's position
    relative to the revocation and the write lock observable."""

    def __init__(self, store, log: List[Any]):
        self._store = store
        self._log = log

    def get_binding(self, issuer: str, subject: str):
        self._log.append(("get_binding", issuer, subject))
        return self._store.get_binding(issuer, subject)

    def __getattr__(self, name):
        return getattr(self._store, name)


@contextmanager
def _revoke_at_the_write_lock(instance, db_path, subject: str, log: List[Any]):
    """Commit a revocation of ``subject`` in the window between ingest's pre-transaction
    work and its ``BEGIN IMMEDIATE`` — the exact precondition rev 6 §8 states.

    The revocation runs on a SECOND connection (a second Station over the same db file),
    as a real administrator's revoke would; it can only commit here because the ingest
    does not yet hold the write lock, which is precisely why this window exists in
    production (manifest verification runs before the lock and is not fast)."""
    real_transaction = instance.store.transaction
    fired: List[bool] = []

    @contextmanager
    def hooked():
        if not fired:
            fired.append(True)
            with _station(db_path) as other:
                assert other.store.revoke_binding(I, subject) is True
            log.append("revoked")
        with real_transaction() as s:
            log.append("write-lock")
            yield s

    instance.store.transaction = hooked
    try:
        yield
    finally:
        del instance.store.transaction


def _steward_supersede_on_alice(st):
    """BOB, bound STEWARD, publishes his own version superseding ALICE's head. Returns
    ``(folio, edge, g, v)``. Admitted while the steward binding is live (S4)."""
    g, v = _victim_lineage(st)
    mp.bind_party(st, mp.BOB, "steward")
    bob_v = h.folio("finding", "steward version", "sv", BOB_TS)
    return bob_v, _thread("supersedes", bob_v["content_hash"], v), g, v


def test_s5_control_a_live_steward_supersede_is_admitted(db):
    """POSITIVE CONTROL for S5: with the steward binding ACTIVE the identical batch is
    ADMITTED. So the refusal in the two tests below is attributable to the revocation
    alone — not to the tier, the shape, the endpoints, or the injected bindings wrapper
    (which is used here too, and changes nothing)."""
    with _station(db) as st:
        bob_v, edge, g, v = _steward_supersede_on_alice(st)
        log: List[Any] = []
        ack = mp.publish_as(st, mp.BOB, [bob_v], [edge],
                            bindings=_RecordingBindings(st.store, log))
        assert bob_v["content_hash"] in ack["accepted"], ack["rejected"]
        assert edge["thread_hash"] in ack["threads"]["accepted"], \
            _reject_reason(ack, edge["thread_hash"])
        assert ("get_binding", I, mp.BOB) in log  # the wrapper really is the seam


def test_s5_revocation_committed_before_the_write_lock_flips_this_ingest(db):
    """S5, deterministic. A revocation commits after ingest starts but before it takes
    BEGIN IMMEDIATE; the live read inside the lock must see it and refuse the whole batch
    with 'revoked binding'.

    KILLS THE HOISTED-READ MUTANT. Measured against that mutant, the damage is NOT where
    a supersedes-shaped test would look: the edge is still refused, because
    `may_act_on_lineage` re-reads the binding itself (the plan's 1+N correction) and
    happens to catch it — but with the WRONG REASON ('no edit access', an authz verdict,
    not 'revoked binding'). What actually lands is the FOLIO: m_bound stays True, so the
    revoked signer's folio is stored AND gets a constituent_attribution row claiming it.
    A revoked account still writing to the corpus and claiming ownership is a claim-1
    violation, and it is invisible to any assertion that only checks the edge was
    refused. Hence the reason is pinned exactly and the folio/attribution are asserted."""
    with _station(db) as st:
        bob_v, edge, g, v = _steward_supersede_on_alice(st)
        log: List[Any] = []
        with _revoke_at_the_write_lock(st, db, mp.BOB, log):
            ack = mp.publish_as(st, mp.BOB, [bob_v], [edge],
                                bindings=_RecordingBindings(st.store, log))

        # PLACEMENT: the revocation committed, then the lock was taken, and only THEN was
        # the binding read. A hoisted read appears at log[0] instead.
        assert log[0] == "revoked", log
        assert log[1] == "write-lock", log
        assert ("get_binding", I, mp.BOB) in log[2:], log

        # THE SPECIFIC REASON. 'revoked binding' — not 'unbound signer' (the row is still
        # there, revoked_at set, B3), not an authz reason. A refusal reading anything else
        # means the batch died for a reason other than the revocation.
        assert edge["thread_hash"] not in ack["threads"]["accepted"]
        assert _reject_reason(ack, edge["thread_hash"]) == "revoked binding"
        # A failed binding rejects EVERY constituent identically — folio as well as
        # thread (the unified-table principle).
        assert ack["accepted"] == [] and ack["existing"] == []
        assert [r["reason"] for r in ack["rejected"]] == ["revoked binding"]

        # Claim 1, on the graph and not just the ack: ALICE's head is untouched, and the
        # revoked signer wrote NOTHING — no folio, no edge, and no attribution row
        # claiming ownership of either. The attribution check is the one that pins the
        # hoisted read's actual damage (see the docstring).
        assert st.store.get_thread(edge["thread_hash"]) is None
        assert st.store.get_folio(bob_v["content_hash"]) is None
        assert st.store.get_threads(to_id=v, type="supersedes") == []
        assert lineage_genesis_for(st.store, v).hash == g
        assert _attributions_to(st.store, mp.BOB) == []


def test_s5_revocation_under_real_write_lock_contention(db):
    """S5 at full rung T: nothing about the ingress is patched. A second connection holds
    BEGIN IMMEDIATE while it revokes BOB, so the publishing connection genuinely BLOCKS in
    BEGIN IMMEDIATE on busy_timeout and reads the binding only after winning the lock.

    Under a rollback journal the revoker's uncommitted UPDATE is invisible to the other
    connection, so a HOISTED read — taken before the block — sees BOB still ACTIVE and
    admits the supersede. The live read, taken after the lock is won, sees the revocation.

    Also the claim-2/liveness observable §2 names for rung T: the blocked writer must WAIT
    on busy_timeout and return a decision, never fail with 'database is locked'."""
    lock_held = threading.Event()
    publishing = threading.Event()

    def revoker():
        with _station(db) as other:
            with other.store.transaction():          # BEGIN IMMEDIATE: takes the lock
                assert other.store.revoke_binding(I, mp.BOB) is True
                lock_held.set()
                # Hold the lock until the publisher is well inside ingest. A hoisted read
                # fires within microseconds of `publishing`; this margin is ~1000x that,
                # and well under the 5000ms busy_timeout the publisher waits on.
                publishing.wait(timeout=10)
                time.sleep(0.4)
            # commit happens on exiting the transaction, releasing the lock

    # The publisher's connection is opened BEFORE the revoker takes the lock: Station()
    # ensures the schema (DDL = a write), which would otherwise contend for the very lock
    # this test wants the INGEST to contend for.
    with _station(db) as st:
        bob_v, edge, g, v = _steward_supersede_on_alice(st)

        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(revoker)
            assert lock_held.wait(timeout=10), "revoker never took the write lock"
            publishing.set()
            ack = mp.publish_as(st, mp.BOB, [bob_v], [edge])
            fut.result(timeout=30)

            # LIVENESS (claim 2, the starvation case §2 names for rung T): reaching this
            # line at all is the evidence — a BEGIN IMMEDIATE that exhausted busy_timeout
            # raises out of transaction() and never returns an ack. The extra check is
            # that no per-item write was degraded to the 'invalid fields' catch-all,
            # which is how a lock error inside the savepoint would surface.
            assert "invalid fields" not in str(ack["rejected"]), ack["rejected"]
            # Safety: the decision is the REVOKED one, on BOTH constituents.
            assert edge["thread_hash"] not in ack["threads"]["accepted"]
            assert _reject_reason(ack, edge["thread_hash"]) == "revoked binding"
            assert ack["accepted"] == [] and ack["existing"] == []
            assert [r["reason"] for r in ack["rejected"]] == ["revoked binding"]
            assert st.store.get_thread(edge["thread_hash"]) is None
            assert st.store.get_folio(bob_v["content_hash"]) is None
            assert st.store.get_threads(to_id=v, type="supersedes") == []
            assert _attributions_to(st.store, mp.BOB) == []
