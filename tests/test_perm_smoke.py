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

Batch 1: S3 (breadcrumb / from-end purity) and S4 (tier escalation, incl. the operator gap).
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
# ============================================================================

def test_s3_contributor_cannot_file_a_victims_folio(instance):
    g, v = _victim_lineage(instance)
    # BOB is a bound originator with a site_contribute grant on HIS OWN site.
    mp.bind_party(instance, mp.BOB, "originator")
    site = h.folio("site", "bob site", "bob's site", BOB_TS)
    mp.publish_as(instance, mp.BOB, [site], [], site_slugs={site["content_hash"]: "bob-site"})
    mp.grant_on(instance, site["content_hash"], mp.BOB, "site_contribute")

    # THE ATTACK: file ALICE's folio into BOB's site (from-end = a folio BOB does not own).
    edge = _thread("within", v, site["content_hash"])
    ack = mp.publish_as(instance, mp.BOB, [], [edge])
    assert edge["thread_hash"] not in ack["threads"]["accepted"]
    assert "cannot originate from a folio you do not hold" in _reject_reason(ack, edge["thread_hash"])

    # POSITIVE CONTROL — isolates the from-end as the sole load-bearing guard: the SAME
    # grant DOES admit BOB filing his OWN folio, so the refusal above is from-end purity,
    # not a broken/missing grant.
    mine = h.folio("finding", "bob member", "m", BOB_TS)
    ok_edge = _thread("within", mine["content_hash"], site["content_hash"])
    ack_ok = mp.publish_as(instance, mp.BOB, [mine], [ok_edge])
    assert ok_edge["thread_hash"] in ack_ok["threads"]["accepted"], ack_ok["threads"]["rejected"]


def test_s3_victims_breadcrumb_is_unchanged_by_the_attack(instance):
    """The observable behind S3 (claim 1): the attack leaves the victim's rendered
    breadcrumb untouched — ALICE's folio never gains BOB's site."""
    from skein.envelope import build_folio_envelope

    g, v = _victim_lineage(instance)
    mp.bind_party(instance, mp.BOB, "originator")
    site = h.folio("site", "bob site", "bob's site", BOB_TS)
    mp.publish_as(instance, mp.BOB, [site], [], site_slugs={site["content_hash"]: "bob-site"})
    mp.grant_on(instance, site["content_hash"], mp.BOB, "site_contribute")

    before = build_folio_envelope(instance.store, v)["asserted"]["site"]
    mp.publish_as(instance, mp.BOB, [], [_thread("within", v, site["content_hash"])])
    after = build_folio_envelope(instance.store, v)["asserted"]["site"]
    assert before == after  # no breadcrumb, before and after — the attack changed nothing
    assert after is None


# ============================================================================
# S4 — Escalate. Every tier acting on a FOREIGN lineage, refused where it should be. The
# to-end right (may_act_on_lineage) is satisfied by a station tier in _TIER_MAY_ACT
# {operator, administrator, steward}; an ORIGINATOR is NOT a moderation tier. The plan:
# the OPERATOR tier has ZERO tests in the authorization path, and administrator is never
# REFUSED anywhere. Both covered here, with a real second signer.
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
    assert len(acc) == 1 and len(rej) == 1, ack["threads"]  # one parent lands, merge refused
    assert "second parent (merge)" in rej[0]["reason"]
    # Claim 2: new_v's lineage still resolves to a single genesis — not bricked.
    assert lineage_genesis_for(instance.store, new_v["content_hash"]).resolved


def test_s7_self_edge_supersedes_refused(instance):
    (a,) = _bob_owns(instance, "A self")
    e = _thread("supersedes", a, a)
    ack = mp.publish_as(instance, mp.BOB, [], [e])
    assert e["thread_hash"] not in ack["threads"]["accepted"]
    assert "self-edge" in _reject_reason(ack, e["thread_hash"])


def test_s7_cycle_closing_edge_refused_and_does_not_brick(instance):
    a, b = _bob_owns(instance, "A cyc", "B cyc")
    c1 = _thread("supersedes", a, b)  # A supersedes B
    c2 = _thread("supersedes", b, a)  # B supersedes A -> would close the cycle
    ack = mp.publish_as(instance, mp.BOB, [], [c1, c2])

    acc, rej = ack["threads"]["accepted"], ack["threads"]["rejected"]
    # One edge stages; the cycle-CLOSING edge is refused at genesis resolution before it
    # can land. (Which edge stages is wire-order-dependent — finding 4 / Q4, bucket 3 —
    # but the cycle itself never fully forms, so no lineage is bricked.)
    assert len(acc) == 1 and len(rej) == 1, ack["threads"]
    assert "unresolvable target lineage" in rej[0]["reason"]
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
    assert "second parent (merge)" in ackm["threads"]["rejected"][0]["reason"]

    # CYCLE under OFF -> NO backstop (distinct from_ids); both edges land and BRICK.
    c1 = _thread("supersedes", a, b)
    c2 = _thread("supersedes", b, a)
    ackc = mp.publish_as(instance, "anon@x", [], [c1, c2], require_signed=False)
    assert len(ackc["threads"]["accepted"]) == 2, ackc["threads"]
    with pytest.raises(LineageReject):
        lineage_genesis_for(instance.store, a)
