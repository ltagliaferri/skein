"""Workstream B — the adversarial multi-party smoke (brief-20260716-93t3 §7), rung 2.

By ATTACKER GOAL, driven by a REAL second signature through ``ingest()`` (the harness
:mod:`tests.perm_multiparty`, which closed Fact 1). These scenarios target the gaps the
plan names: coverage that exists only at rung 0 (the blind unit layer, where "party 2" is
a pre-seeded attribution row and every negative test refuses because ``get_binding``
returns ``None``, not because the tier lacks power) is re-driven here with a genuinely
bound second party, and the wholly-untested surfaces (the OPERATOR tier, §7 S4) are
covered for the first time.

Every scenario asserts the SPECIFIC reject reason (§3 rule 1: a right-answer-wrong-reason
refusal is a test bug wearing a pass) and carries a negative/positive control that isolates
the ONE guard claimed load-bearing, so an "attack -> refused" is distinguished from "I
built a malformed request."

Batch 1: S3 (breadcrumb / from-end purity), S4 (tier escalation via supersedes, incl. the
operator gap), and S7 (bricking / claim 2 / the guard asymmetry).
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from skein.identity import compute_thread_hash
from skein.station import Station
from skein.thread_authz import _TIER_MAY_ACT, LineageReject, lineage_genesis_for
from tests import perm_multiparty as mp
from tests import station_publish_helpers as h

_TS = "2026-01-01T00:00:00+00:00"
BOB_TS = "2026-06-01T00:00:00+00:00"


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein-next")
    yield s
    s.close()


def _thread(ttype: str, frm: str, to: str, created_at: str = BOB_TS) -> Dict[str, Any]:
    th = compute_thread_hash(
        from_id=frm, to_id=to, type=ttype, weaver=None, created_at=created_at, content=None,
    )
    return {
        "thread_hash": th, "from_id": frm, "to_id": to, "type": ttype,
        "weaver": None, "created_at": created_at, "content": None,
    }


def _victim_lineage(instance):
    """ALICE (bound originator) owns genesis G and head V (V supersedes G). Returns
    ``(G_hash, V_hash)``. The lineage an attacker tries to seize or brick."""
    mp.bind_party(instance, mp.ALICE, "originator")
    g = h.folio("finding", "alice genesis", "g", _TS)
    v = h.folio("finding", "alice head", "v", "2026-01-02T00:00:00+00:00")
    a1 = mp.publish_as(instance, mp.ALICE, [g], [])
    a2 = mp.publish_as(instance, mp.ALICE, [v],
                       [_thread("supersedes", v["content_hash"], g["content_hash"],
                                "2026-01-02T00:00:00+00:00")])
    assert not a1["rejected"] and not a2["threads"]["rejected"], (a1, a2)
    return g["content_hash"], v["content_hash"]


def _reject_reason(ack, thread_hash) -> str:
    rej = [r for r in ack["threads"]["rejected"] if r["thread_hash"] == thread_hash]
    return rej[0]["reason"] if rej else "<accepted or absent>"


# ============================================================================
# S3 — Poison a breadcrumb (from-end purity). A per-document grant satisfies the
# TO-end right, NEVER the from-end (§5.1). So a contributor can file HIS OWN folio into a
# site he may contribute to, but can NEVER file a VICTIM's folio there — which would
# otherwise re-home the victim's breadcrumb (folio_site_slug, the alphabetically-first
# slug) into the attacker's site. rung 0 tests this BLIND; here BOB really signs.
#
# The site is owned by ALICE, not BOB — so BOB's admission genuinely rides the GRANT
# (may_act_on_lineage via has_active_grant), not ownership of a site he happens to hold.
# The fell caught the earlier version giving BOB his own site, where owns_folio short-
# circuited the grant and the from-end-purity property was never actually exercised.
# ============================================================================

def _alice_site(instance) -> str:
    """ALICE (already a bound originator) owns a site 'alice-site'. Returns its genesis."""
    site = h.folio("site", "alice site", "alice's site", "2026-01-03T00:00:00+00:00")
    ack = mp.publish_as(instance, mp.ALICE, [site], [],
                        site_slugs={site["content_hash"]: "alice-site"})
    assert site["content_hash"] in ack["accepted"], ack["rejected"]
    return site["content_hash"]


def test_s3_a_grant_satisfies_the_to_end_but_never_the_from_end(instance):
    """From-end purity (§5.1), fully isolated on the GRANT path: the site is ALICE's, so
    BOB acts only through the grant. (a) grant NECESSARY, (b) grant SUFFICIENT for the
    to-end, (c) grant NEVER reaches the from-end — the breadcrumb poison, blocked."""
    g, v = _victim_lineage(instance)
    site = _alice_site(instance)               # owned by ALICE; BOB does NOT own it
    mp.bind_party(instance, mp.BOB, "originator")

    # (a) NECESSARY: without the grant, BOB filing his OWN folio is refused at the to-end.
    m1 = h.folio("finding", "bob member A", "m", BOB_TS)
    e1 = _thread("within", m1["content_hash"], site)
    a1 = mp.publish_as(instance, mp.BOB, [m1], [e1])
    assert e1["thread_hash"] not in a1["threads"]["accepted"]
    assert "no contribute access" in _reject_reason(a1, e1["thread_hash"])

    # (b) SUFFICIENT for the to-end: with the grant, BOB may file his OWN folio.
    mp.grant_on(instance, site, mp.BOB, "site_contribute")
    m2 = h.folio("finding", "bob member B", "m", "2026-06-02T00:00:00+00:00")
    e2 = _thread("within", m2["content_hash"], site)
    a2 = mp.publish_as(instance, mp.BOB, [m2], [e2])
    assert e2["thread_hash"] in a2["threads"]["accepted"], a2["threads"]["rejected"]

    # (c) NEVER the from-end: with the SAME active grant, filing ALICE's folio is still
    # refused — the grant satisfies the to-end but owns_folio gates the from-end.
    e3 = _thread("within", v, site)
    a3 = mp.publish_as(instance, mp.BOB, [], [e3])
    assert e3["thread_hash"] not in a3["threads"]["accepted"]
    assert "cannot originate from a folio you do not hold" in _reject_reason(a3, e3["thread_hash"])


def test_s3_victims_breadcrumb_is_unchanged_by_the_attack(instance):
    """The observable behind S3 (claim 1): even holding a live site_contribute grant, the
    attack leaves the victim's rendered breadcrumb untouched — ALICE's folio never gains
    the site the attacker could contribute to."""
    from skein.envelope import build_folio_envelope

    g, v = _victim_lineage(instance)
    site = _alice_site(instance)
    mp.bind_party(instance, mp.BOB, "originator")
    mp.grant_on(instance, site, mp.BOB, "site_contribute")  # BOB may contribute to alice-site

    before = build_folio_envelope(instance.store, v)["asserted"]["site"]
    mp.publish_as(instance, mp.BOB, [], [_thread("within", v, site)])  # attack: file ALICE's V
    after = build_folio_envelope(instance.store, v)["asserted"]["site"]
    assert before == after  # no breadcrumb, before and after — the attack changed nothing
    assert after is None


# ============================================================================
# S4 — Escalate (via supersedes; batch 1). Each tier acting on a FOREIGN lineage, refused
# where it should be. The to-end right (may_act_on_lineage) is satisfied by a station tier
# in _TIER_MAY_ACT {operator, administrator, steward}; an ORIGINATOR is NOT a moderation
# tier. The plan: the OPERATOR tier has ZERO tests in the authorization path, and
# administrator is never REFUSED anywhere. Both covered here, with a real second signer.
# (The full tier x class matrix — status/reverted/archive/assignment — is a later batch;
# this batch pins the tier boundary on supersedes, which is the moderation predicate the
# other to-end classes route through.)
# ============================================================================

@pytest.mark.parametrize("role,admits", [
    ("originator", False),      # the escalation FLOOR — a bound collaborator is not a moderator
    ("steward", True),
    ("administrator", True),    # the plan: never refused anywhere before
    ("operator", True),         # the plan: ZERO tests in the authz path before this
])
def test_s4_tier_may_supersede_a_foreign_lineage(instance, role, admits):
    g, v = _victim_lineage(instance)
    mp.bind_party(instance, mp.BOB, role)
    # BOB creates his own new version and tries to supersede ALICE's head onto her lineage.
    bob_v = h.folio("finding", "bob version", "bv", BOB_TS)
    edge = _thread("supersedes", bob_v["content_hash"], v)
    ack = mp.publish_as(instance, mp.BOB, [bob_v], [edge])
    if admits:
        assert edge["thread_hash"] in ack["threads"]["accepted"], _reject_reason(ack, edge["thread_hash"])
    else:
        assert edge["thread_hash"] not in ack["threads"]["accepted"]
        reason = _reject_reason(ack, edge["thread_hash"])
        assert "no edit access" in reason and g in reason  # refused at the resolved GENESIS
        # Observable (claim 1): the refused attack left ALICE's lineage untouched — the
        # edge is not stored and V is still the head (nothing supersedes it).
        assert instance.store.get_thread(edge["thread_hash"]) is None
        assert not instance.store.get_threads(to_id=v, type="supersedes")


def test_s4_originator_is_not_in_the_moderation_tier_set():
    """Pins the escalation floor at the source: the mutant that adds 'originator' to
    _TIER_MAY_ACT (the plan's worst regression — silently promoting every collaborator to
    steward) would flip test_s4_tier[...originator...] to admit; this guards the constant
    directly so the intent is legible even if the parametrization drifts."""
    assert "originator" not in _TIER_MAY_ACT
    assert _TIER_MAY_ACT == {"operator", "administrator", "steward"}


def test_s4_tier_power_is_to_end_only_even_for_operator(instance):
    """The from-end purity BOUNDARY at the highest tier: a tier satisfies the TO-end
    (may_act_on_lineage) but NEVER the from-end (owns_folio). So even an OPERATOR cannot
    ORIGINATE a supersedes from a folio it does not own — the seam a tier must not cross."""
    g, v = _victim_lineage(instance)
    mp.bind_party(instance, mp.BOB, "operator")

    # Operator CAN moderate onto ALICE's lineage from operator's OWN new version (to-end).
    bob_v = h.folio("finding", "op version", "ov", BOB_TS)
    ok = _thread("supersedes", bob_v["content_hash"], v)
    ack_ok = mp.publish_as(instance, mp.BOB, [bob_v], [ok])
    assert ok["thread_hash"] in ack_ok["threads"]["accepted"], _reject_reason(ack_ok, ok["thread_hash"])

    # But operator CANNOT originate a supersedes FROM ALICE's folio (from-end purity):
    # a fresh ALICE-owned folio the operator does not hold, used as the from-end (ALICE is
    # already a bound originator from _victim_lineage).
    other = h.folio("finding", "alice other", "ao", "2026-01-03T00:00:00+00:00")
    mp.publish_as(instance, mp.ALICE, [other], [])
    bad = _thread("supersedes", other["content_hash"], g)
    ack_bad = mp.publish_as(instance, mp.BOB, [], [bad])
    assert bad["thread_hash"] not in ack_bad["threads"]["accepted"]
    assert "cannot originate from a folio you do not hold" in _reject_reason(ack_bad, bad["thread_hash"])


# ============================================================================
# S7 — Deny a legitimate action (claim 2: nothing good is prevented). Bricking-shaped
# batches — merge, self-edge, cycle — must be REFUSED before they land: a landed merge or
# cycle makes the genesis resolver fail closed BEFORE any tier check, so the lineage is
# unrecoverable "beyond even the operator" (§7). Under ON the authz cycle/merge checks
# close this; the GUARD ASYMMETRY is that a merge ALSO has a DB backstop (the <=1-parent
# partial-unique index on threads(from_id)) while a cycle (two distinct from_ids) has NONE
# — so under OFF a cycle can still brick, which is the residual claim-2 risk.
# ============================================================================

def _bob_owns(instance, *titles):
    """BOB (bound originator) publishes one genesis folio per title; returns their hashes."""
    mp.bind_party(instance, mp.BOB, "originator")
    out = []
    for i, t in enumerate(titles):
        f = h.folio("finding", t, "b", f"2026-06-{i + 1:02d}T00:00:00+00:00")
        ack = mp.publish_as(instance, mp.BOB, [f], [])
        assert f["content_hash"] in ack["accepted"], ack["rejected"]
        out.append(f["content_hash"])
    return out


def test_s7_merge_is_refused_and_does_not_brick(instance):
    a, b = _bob_owns(instance, "A genesis", "B genesis")
    new_v = h.folio("finding", "merge child", "m", BOB_TS)
    e1 = _thread("supersedes", new_v["content_hash"], a)
    e2 = _thread("supersedes", new_v["content_hash"], b)  # a SECOND parent -> a merge
    ack = mp.publish_as(instance, mp.BOB, [new_v], [e1, e2])

    acc, rej = ack["threads"]["accepted"], ack["threads"]["rejected"]
    assert acc == [e1["thread_hash"]], ack["threads"]        # the first parent lands
    assert [r["thread_hash"] for r in rej] == [e2["thread_hash"]]  # the SECOND parent refused
    # Assert the ON AUTHZ reason ("would GIVE ..."), which is DISTINCT from the OFF index
    # backstop's "would CREATE ..." — so an authz-check regression rescued by the index
    # would fail here rather than pass on the shared "(merge)" tail (fell r1, codex).
    assert "would give the new version a second parent (merge)" in rej[0]["reason"]
    # Claim 2: new_v's lineage still resolves to a single genesis — not bricked.
    assert lineage_genesis_for(instance.store, new_v["content_hash"]).resolved


def test_s7_self_edge_supersedes_refused(instance):
    (a,) = _bob_owns(instance, "A self")
    e = _thread("supersedes", a, a)
    ack = mp.publish_as(instance, mp.BOB, [], [e])
    assert e["thread_hash"] not in ack["threads"]["accepted"]
    assert "self-edge" in _reject_reason(ack, e["thread_hash"])
    # GRAPH side effect, not just the ack: the edge is not stored and the lineage still
    # resolves — a reject that appended a reason but failed to `continue` before save_thread
    # would leave supersedes(a,a) stored and brick the lineage (fell r1, codex).
    assert instance.store.get_thread(e["thread_hash"]) is None
    assert lineage_genesis_for(instance.store, a).resolved


def test_s7_cycle_closing_edge_refused_and_does_not_brick(instance):
    a, b = _bob_owns(instance, "A cyc", "B cyc")
    c1 = _thread("supersedes", a, b)  # A supersedes B
    c2 = _thread("supersedes", b, a)  # B supersedes A -> would close the cycle
    ack = mp.publish_as(instance, mp.BOB, [], [c1, c2])

    acc, rej = ack["threads"]["accepted"], ack["threads"]["rejected"]
    # The Q4-INDEPENDENT claim-2 invariant: AT MOST one edge of a cyclic batch lands, so
    # the cycle never fully forms and NEITHER lineage is bricked. Asserting `<= 1` (not
    # `== 1`) deliberately does not pin how many edges stage — that is finding 4 / Q4,
    # bucket 3, Patrick's call: today one stages by wire order, but were a cyclic batch
    # ruled to reject wholesale (acc == 0), this no-brick property must still hold (fell
    # r1, opus). At least one edge is refused at genesis resolution.
    assert len(acc) <= 1, ack["threads"]
    assert any("unresolvable target lineage" in r["reason"] for r in rej), rej
    assert lineage_genesis_for(instance.store, a).resolved
    assert lineage_genesis_for(instance.store, b).resolved


def test_s7_guard_asymmetry_merge_backstopped_cycle_not_under_off(instance):
    """The guard asymmetry (§7) and the residual claim-2 risk, made empirical. Under OFF
    the authz path is skipped (ingress is manifest+binding blind), so the ONLY backstop is
    the <=1-parent partial-unique index on threads(from_id): it catches a MERGE (two
    parents share one from_id) but NOT a CYCLE (two DISTINCT from_ids). So under OFF a
    cycle lands and bricks the lineage — exactly what the ON authz cycle-check prevents."""
    a_f = h.folio("finding", "A off", "a", "2026-06-01T00:00:00+00:00")
    b_f = h.folio("finding", "B off", "b", "2026-06-02T00:00:00+00:00")
    ack0 = mp.publish_as(instance, "anon@x", [a_f, b_f], [], require_signed=False)
    a, b = a_f["content_hash"], b_f["content_hash"]
    assert a in ack0["accepted"] and b in ack0["accepted"]

    # MERGE under OFF -> the partial-unique index still refuses the second parent.
    new_v = h.folio("finding", "merge off", "m", BOB_TS)
    m1 = _thread("supersedes", new_v["content_hash"], a)
    m2 = _thread("supersedes", new_v["content_hash"], b)
    ackm = mp.publish_as(instance, "anon@x", [new_v], [m1, m2], require_signed=False)
    assert len(ackm["threads"]["accepted"]) == 1 and len(ackm["threads"]["rejected"]) == 1
    # The INDEX backstop's reason ("would CREATE ...") — distinct from the ON authz
    # "would GIVE ..." asserted in test_s7_merge — proving this refusal came from the DB
    # index under OFF, not the authz check (which never runs under OFF).
    assert "would create a second parent (merge)" in ackm["threads"]["rejected"][0]["reason"]

    # CYCLE under OFF -> NO backstop (distinct from_ids); both edges land and BRICK.
    c1 = _thread("supersedes", a, b)
    c2 = _thread("supersedes", b, a)
    ackc = mp.publish_as(instance, "anon@x", [], [c1, c2], require_signed=False)
    assert len(ackc["threads"]["accepted"]) == 2, ackc["threads"]
    with pytest.raises(LineageReject):
        lineage_genesis_for(instance.store, a)
