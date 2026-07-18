"""Workstream B — the adversarial multi-party smoke, batch 2 (brief-20260716-93t3 §7).

Rung 2, by ATTACKER GOAL, driven by a REAL second signature through ``ingest()`` (the
harness :mod:`tests.perm_multiparty`, which closed Fact 1). Batch 1 (test_perm_smoke.py)
covered S3/S4/S7; this batch covers the three the plan names as *unreached*:

  S8  GET IN THROUGH A FORBIDDEN SHAPE — the clause-(b) shape diff at the INGRESS. The
      plan says "a from-end alias is tested for no class at all"; that overstates the
      gap. Rung 0 already drives a NON-HASH from-end for the CO-DEFENDED classes —
      status/archive (test_perm_authorize_thread.py:269/290, agent-origin -> self-loop)
      and message/succession (:466/472, agent-origin) — and refuses ``published`` on its
      CLASS before any shape check (:481). What is genuinely untested is (i) the 12 types
      carrying a CONTENT-HASH from-end, which have no from-end shape test at any rung,
      and (ii) the INGRESS level: no from-end shape has ever been driven through real
      ``ingest()`` as a bound second signer. Both are closed here — all 17 types are
      swept, each asserting the EXACT reason its class produces, so the 5 co-defended
      cases are pinned to the guard that really fires rather than to a bare refusal.

  S1  SEIZE A NAME — the slug claim under ON, where naming follows OWNERSHIP OF THE
      ANCHOR and a signer-PAIR collision decides (§3.3/§6). The plan's gap: "operator
      override (only administrator is tested)" — and every existing collision test
      models party 2 as a pre-seeded ``claim_slug`` row, never a second signer really
      publishing. Both closed here.

  S6  CAPTURE A LEGACY LINEAGE — "the unresolved-genesis contract, which no test reaches
      at all." Deleting both ``if genesis.resolved:`` guards in ``may_act_on_lineage``
      leaves all 130 tests green, and an instrumented run found the mutant never diverged
      across 42 calls. These tests construct a genuinely UNRESOLVED genesis
      (``Genesis(resolved=False)``) and reach it through the real authorization path.
      See the S6 header for which of the two guards is killable and which is provably an
      equivalent mutant — the plan's "both" is one guard too many.

Every scenario asserts the SPECIFIC reject reason (§3 rule 1) and carries a
negative/positive control isolating the ONE guard claimed load-bearing, so "attack ->
refused" is distinguished from "I built a malformed request."
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from skein.authorization import STATION_ROLES, Principal
from skein.identity import compute_thread_hash
from skein.station import Station
from skein.thread_authz import (
    Genesis,
    known_thread_types,
    lineage_genesis_for,
    lineage_owner,
    may_act_on_lineage,
)
from tests import perm_multiparty as mp
from tests import station_publish_helpers as h

_TS = "2026-01-01T00:00:00+00:00"
BOB_TS = "2026-06-01T00:00:00+00:00"

# THE CANONICAL NON-CONTENT-HASH FROM-END for the per-type sweep: a legacy folio ALIAS,
# the shape §5.1 names outright (an alias resolves through the MUTABLE aliases table to a
# target the signature never fixed). An agent id ("burr-0715") and a site slug ("specs")
# are the SAME `_CONTENT_ADDRESS_RE.match` failure spelled differently, so sweeping all
# three across all 17 types re-ran one branch ~50 times. The sweep's job is the
# type -> CLASS DISPATCH across the ~5 `_require_content_hash` call sites; the regex's own
# boundary is a separate axis, driven once below where it actually discriminates.
_ALIAS_FROM = "finding-20260101-abcd"

# THE HASH-REGEX BOUNDARY, on one content-hash-from-end type. Each entry breaks a
# DIFFERENT piece of `^(?:sha256::)?[0-9a-f]{64}$` — so a widened regex (a relaxed length,
# a stray re.IGNORECASE, a dropped `$`) fails here instead of admitting a forged endpoint.
_NEAR_MISS_HASHES = [
    "a" * 63,                  # one hex SHORT — the {64} lower bound
    "a" * 65,                  # one hex LONG — the {64} upper bound
    "A" * 64,                  # right length, UPPERCASE — [0-9a-f] is lowercase-only
    "a" * 64 + "-forged",      # a full-length hash with a SUFFIX — the trailing `$`
    "sha256::" + "a" * 63,     # the short hex under the OPTIONAL frame — framing relaxes nothing
]


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


def _reject_reason(ack, thread_hash) -> str:
    rej = [r for r in ack["threads"]["rejected"] if r["thread_hash"] == thread_hash]
    return rej[0]["reason"] if rej else "<accepted or absent>"


def _publish_folio(instance, subject, title, key, ts, ftype="finding", slug=None):
    """One folio published AS ``subject`` through real ingest; returns its hash."""
    f = h.folio(ftype, title, key, ts)
    slugs = {f["content_hash"]: slug} if slug else None
    ack = mp.publish_as(instance, subject, [f], [], site_slugs=slugs)
    assert f["content_hash"] in ack["accepted"] or f["content_hash"] in ack["existing"], ack
    return f["content_hash"]


# ============================================================================
# S8 — GET IN THROUGH A FORBIDDEN SHAPE.
#
# The from-end shape guard, per class, at the ingress. rev 6 §5.1 rejects a non-content-
# address endpoint on the signed path: an alias resolves through the MUTABLE aliases table
# to a target the signature never fixed, and canonicalizing at ingress would break the
# signed thread_hash.
#
# WHAT IS ACTUALLY NEW HERE (the plan's "a from-end alias is tested for no class at all"
# is one class too broad — see the module docstring):
#   - The 12 CONTENT-HASH-FROM-END types (AFFECTING / POINTER / reverted / ASSIGNMENT /
#     ATTRIBUTION). Rung 0 pins only the TO-end shape (supersedes + pointer,
#     test_perm_authorize_thread.py:104/190); for these types NO rung tests the FROM-end.
#     This is the load-bearing sweep: it pins type -> class dispatch across the ~5
#     `_require_content_hash(frm, ...)` call sites, so a type re-classified into a class
#     that skips the from-end check fails here.
#   - The INGRESS LEVEL, for all 17. Rung 0 calls authorize_thread directly; nothing has
#     driven a from-end shape through real ingest() as a bound second signer, where the
#     wire layer, the batch transaction and the staged graph all sit in front of the guard.
# The 5 CO-DEFENDED types are already covered at rung 0 (status/archive :269/290,
# message/succession :466/472, published :481) — they ride along to pin the co-defending
# guard at the ingress, not because their class is untested.
#
# THE CO-DEFENDING GUARDS (§3 rule 2: record the guard stack). Three classes never reach
# _require_content_hash on the from-end, and asserting the mere refusal would hide that:
#   - status/archive (CONTROL self-loop): the `from != to` SELF-LOOP check fires FIRST,
#     so the reason is the self-loop one. The content-hash check still runs, but on to_id.
#   - message/succession (NON_FOLIO): no _require_content_hash at all — a non-hash from-end
#     falls to the AGENT-ORIGIN reject, which is the stronger statement (no station
#     principal authorizes an agent origin day-one).
#   - published (WIRE_REJECT): refused before any shape check; it never travels.
# ============================================================================

_FROM_HASH = "from_id must be a content hash, not an alias or slug"
_SELF_LOOP = "must be a self-loop (from_id == to_id)"
_AGENT_ORIGIN = "no station principal authorizes an agent-origin thread"
_NO_TRAVEL = "does not travel the published graph"

# type -> the reason a NON-CONTENT-HASH from-end produces, derived from the §2 class
# table. The from-end shape guard is the load-bearing one for the first 12; the last 5
# are the co-defended cases above, named explicitly so a regression that swapped a guard
# for a weaker one fails here instead of passing on "it was refused".
_FROM_END_REJECT = {
    # AFFECTING
    "supersedes": _FROM_HASH,
    "within": _FROM_HASH,
    # POINTER
    "reference": _FROM_HASH,
    "reply": _FROM_HASH,
    "mention": _FROM_HASH,
    "forks": _FROM_HASH,
    "responds_to": _FROM_HASH,
    "imports_legacy": _FROM_HASH,
    "tag": _FROM_HASH,
    # CONTROL — reverted checks the from-end; status/archive are co-defended by self-loop
    "reverted": _FROM_HASH,
    "status": _SELF_LOOP,
    "archive": _SELF_LOOP,
    # ASSIGNMENT / ATTRIBUTION
    "assignment": _FROM_HASH,
    "attribution": _FROM_HASH,
    # NON_FOLIO — co-defended by the agent-origin reject
    "message": _AGENT_ORIGIN,
    "succession": _AGENT_ORIGIN,
    # WIRE_REJECT — refused before any shape check
    "published": _NO_TRAVEL,
}


@pytest.fixture
def corpus(instance):
    """BOB, a bound originator, owning everything: a 2-version lineage G <- V2 (V2 is the
    head), a spare version V3, a site genesis, and a member folio. BOB owns every anchor,
    so NO to-end right is ever the thing refusing — the shape guard is isolated."""
    mp.bind_party(instance, mp.BOB, "originator")
    g = _publish_folio(instance, mp.BOB, "bob genesis", "g", BOB_TS)
    v2 = _publish_folio(instance, mp.BOB, "bob v2", "v2", "2026-06-02T00:00:00+00:00")
    ack = mp.publish_as(instance, mp.BOB, [], [_thread("supersedes", v2, g)])
    assert not ack["threads"]["rejected"], ack["threads"]
    v3 = _publish_folio(instance, mp.BOB, "bob v3", "v3", "2026-06-03T00:00:00+00:00")
    site = _publish_folio(instance, mp.BOB, "bob site", "s", "2026-06-04T00:00:00+00:00",
                          ftype="site")
    member = _publish_folio(instance, mp.BOB, "bob member", "m", "2026-06-05T00:00:00+00:00")
    return {"g": g, "v2": v2, "v3": v3, "site": site, "member": member}


def _well_shaped(corpus, ttype):
    """The ADMISSIBLE thread of ``ttype`` for the corpus — the positive control. Its
    from-end is a real content hash BOB owns; everything else matches the attack thread.
    ``published`` has none by construction (WIRE_REJECT: it never travels), which is
    itself the contract, so it maps to None."""
    g, v2, v3 = corpus["g"], corpus["v2"], corpus["v3"]
    site, member = corpus["site"], corpus["member"]
    return {
        "supersedes": (v3, v2),          # a new version onto the head; genesis G, owned
        "within": (member, site),        # member into BOB's own site genesis
        "reference": (member, g), "reply": (member, g), "mention": (member, g),
        "forks": (member, g), "responds_to": (member, g), "imports_legacy": (member, g),
        "tag": (member, g),
        "reverted": (v2, g),             # from the HEAD back to a reachable ancestor
        "status": (g, g), "archive": (g, g),      # self-loops on the genesis
        "assignment": (g, "burr-0715"),  # from the genesis, to an agent
        "attribution": (member, "burr-0715"),
        "message": (member, g), "succession": (member, g),
        "published": None,
    }[ttype]


def _attack_to_end(corpus, ttype):
    """The to-end the attack thread uses — the SAME one its positive control uses, so the
    only difference between the two threads is the from-end under test."""
    good = _well_shaped(corpus, ttype)
    return corpus["g"] if good is None else good[1]


def test_s8_taxonomy_is_fully_covered_by_the_from_end_table():
    """The table above is a hand-written enumeration (§2's repeated lesson), so pin it to
    the taxonomy in BOTH directions: a new thread type — or a deleted one — fails here
    instead of silently going untested for a from-end shape."""
    assert set(_FROM_END_REJECT) == set(known_thread_types())


@pytest.mark.parametrize("ttype", sorted(_FROM_END_REJECT))
def test_s8_non_content_hash_from_end_refused_per_class(instance, corpus, ttype):
    """S8, the TYPE -> CLASS DISPATCH sweep: a FROM-end that is a legacy alias is refused
    for every one of the 17 types, with the reason that type's guard stack produces. One
    from-end shape, all 17 types — the axis under test is which guard each type reaches,
    not which spelling of a non-hash is rejected (that is the boundary test below)."""
    to = _attack_to_end(corpus, ttype)
    bad = _thread(ttype, _ALIAS_FROM, to)
    ack = mp.publish_as(instance, mp.BOB, [], [bad])

    assert bad["thread_hash"] not in ack["threads"]["accepted"], ack["threads"]
    assert _FROM_END_REJECT[ttype] in _reject_reason(ack, bad["thread_hash"])
    # Observable (claim 1): refused at the ack AND absent from the graph — a reject that
    # appended a reason but failed to `continue` before save_thread would still store it.
    assert instance.store.get_thread(bad["thread_hash"]) is None


@pytest.mark.parametrize("near_miss", _NEAR_MISS_HASHES)
def test_s8_from_end_hash_shape_boundary(instance, corpus, near_miss):
    """The OTHER S8 axis: the content-address regex's own boundary, driven on ONE type
    that really shape-checks its from-end (supersedes, AFFECTING). Each near-miss is a
    hash an attacker could plausibly present — right-length uppercase, one hex off,
    a real hash with a suffix — and each breaks a different piece of
    `^(?:sha256::)?[0-9a-f]{64}$`, so a widened regex cannot pass unnoticed.

    Isolating: the type, the to-end and the signer are identical to the admitting
    supersedes in test_s8_positive_control_same_thread_with_a_real_from_end_admits, whose
    from-end IS a real content hash — so the from-end SHAPE is the only variable."""
    to = _attack_to_end(corpus, "supersedes")
    bad = _thread("supersedes", near_miss, to)
    ack = mp.publish_as(instance, mp.BOB, [], [bad])

    assert bad["thread_hash"] not in ack["threads"]["accepted"], ack["threads"]
    assert _FROM_HASH in _reject_reason(ack, bad["thread_hash"])
    assert instance.store.get_thread(bad["thread_hash"]) is None


@pytest.mark.parametrize("ttype", sorted(t for t in _FROM_END_REJECT if t != "published"))
def test_s8_positive_control_same_thread_with_a_real_from_end_admits(instance, corpus, ttype):
    """The positive control for the test above, per type: swap ONLY the from-end for a
    content hash BOB owns and the identical edge is ADMITTED — so the refusals above are
    a from-end guard doing its job, not a malformed request or a missing to-end right.

    WHICH guard, per type: for the 15 types whose admitting form keeps a from != to shape,
    the swap isolates the from-end SHAPE guard (or, for message/succession, the agent-
    origin guard that stands in for it). For status/archive it does NOT: their admitting
    form is the self-loop (g, g), so swapping the from-end also makes from == to and this
    control isolates the SELF-LOOP guard instead. The from-end shape guard on those two is
    never reached — that is exactly what the _FROM_END_REJECT table records, and
    test_s8_non_self_loop_control_edge_refused pins the self-loop guard on its own.
    (``published`` is excluded: WIRE_REJECT means no admissible instance exists — that IS
    its contract, and the parametrized test above asserts it.)"""
    frm, to = _well_shaped(corpus, ttype)
    good = _thread(ttype, frm, to, "2026-07-01T00:00:00+00:00")
    ack = mp.publish_as(instance, mp.BOB, [], [good])
    assert good["thread_hash"] in ack["threads"]["accepted"], _reject_reason(
        ack, good["thread_hash"]
    )


def test_s8_published_never_travels_even_perfectly_shaped(instance, corpus):
    """The ``published`` control, stated on its own since it has no admitting form: a
    perfectly-shaped published edge from a folio BOB owns onto his own genesis is STILL
    refused. Its class, not its shape, is what refuses it."""
    edge = _thread("published", corpus["member"], corpus["g"])
    ack = mp.publish_as(instance, mp.BOB, [], [edge])
    assert edge["thread_hash"] not in ack["threads"]["accepted"]
    assert _NO_TRAVEL in _reject_reason(ack, edge["thread_hash"])
    assert instance.store.get_thread(edge["thread_hash"]) is None


def test_s8_unknown_thread_type_refused_at_ingress(instance, corpus):
    """The classifier is fail-closed (§5.1/§8): a type absent from the taxonomy is a
    REJECT, never a silent default to a permissive class. Driven at the INGRESS, where
    the wire layer only checks hash integrity (wire.thread_reject_reason) — so an
    unclassified type genuinely reaches authorize_thread and must be refused THERE."""
    bad = _thread("brand_new_type", corpus["member"], corpus["g"])
    ack = mp.publish_as(instance, mp.BOB, [], [bad])
    assert bad["thread_hash"] not in ack["threads"]["accepted"]
    assert "unknown thread type 'brand_new_type'" in _reject_reason(ack, bad["thread_hash"])
    assert instance.store.get_thread(bad["thread_hash"]) is None
    # Positive control: the byte-identical edge under a KNOWN type is admitted, so the
    # refusal is the classifier and not the endpoints.
    good = _thread("reference", corpus["member"], corpus["g"])
    ack2 = mp.publish_as(instance, mp.BOB, [], [good])
    assert good["thread_hash"] in ack2["threads"]["accepted"], _reject_reason(
        ack2, good["thread_hash"]
    )


@pytest.mark.parametrize("ttype", ["status", "archive"])
def test_s8_non_self_loop_control_edge_refused(instance, corpus, ttype):
    """status/archive are self-loops ANCHORED AT THE GENESIS (§5.4) — control state stays
    single-valued per lineage. A two-folio control edge (from != to), with BOTH endpoints
    real content hashes BOB owns and holds, is refused on SHAPE. This is the from!=to case
    with no alias involved, so the self-loop guard is isolated from the shape guard the
    parametrized from-end test exercises."""
    bad = _thread(ttype, corpus["member"], corpus["g"])
    ack = mp.publish_as(instance, mp.BOB, [], [bad])
    assert bad["thread_hash"] not in ack["threads"]["accepted"]
    assert f"{ttype} must be a self-loop (from_id == to_id)" == _reject_reason(
        ack, bad["thread_hash"]
    )
    assert instance.store.get_thread(bad["thread_hash"]) is None

    # Positive control: the self-loop on the genesis, same signer, is admitted.
    good = _thread(ttype, corpus["g"], corpus["g"], "2026-07-01T00:00:00+00:00")
    ack2 = mp.publish_as(instance, mp.BOB, [], [good])
    assert good["thread_hash"] in ack2["threads"]["accepted"], _reject_reason(
        ack2, good["thread_hash"]
    )


def test_s8_reverted_from_a_superseded_version_refused(instance, corpus):
    """reverted's from_id must be the CURRENT HEAD (§5.4): a revert runs from the head
    back to an ancestor, so a revert originating at an already-SUPERSEDED version is
    malformed — it would apply control state from a version the lineage has moved past.
    Driven at the ingress with a real second signer, on a lineage BOB owns outright (so
    no to-end right is what refuses)."""
    g, v2, v3 = corpus["g"], corpus["v2"], corpus["v3"]
    # Deepen the lineage: V3 supersedes V2, so V2 is no longer the head. G <- V2 <- V3.
    ack0 = mp.publish_as(instance, mp.BOB, [], [_thread("supersedes", v3, v2)])
    assert not ack0["threads"]["rejected"], ack0["threads"]

    bad = _thread("reverted", v2, g, "2026-07-01T00:00:00+00:00")   # from the NON-head
    ack = mp.publish_as(instance, mp.BOB, [], [bad])
    assert bad["thread_hash"] not in ack["threads"]["accepted"]
    assert f"reverted from_id {v2} is not the current head" in _reject_reason(
        ack, bad["thread_hash"]
    )
    assert instance.store.get_thread(bad["thread_hash"]) is None

    # Positive control: the SAME revert target from the CURRENT head is admitted — so the
    # refusal is the head check, not the target, the owner, or the lineage.
    good = _thread("reverted", v3, g, "2026-07-02T00:00:00+00:00")
    ack2 = mp.publish_as(instance, mp.BOB, [], [good])
    assert good["thread_hash"] in ack2["threads"]["accepted"], _reject_reason(
        ack2, good["thread_hash"]
    )


def test_s8_assignment_from_a_head_refused(instance, corpus):
    """assignment's from_id must be the lineage GENESIS (§5.1), never a head — a
    genesis-keyed assignment reducer keys on the anchor the design mandates, so an
    assignment anchored on a head would be invisible to it (or double-count). The corpus
    head V2 supersedes G, and BOB owns both, so ownership is not what refuses."""
    g, v2 = corpus["g"], corpus["v2"]
    bad = _thread("assignment", v2, "burr-0715", "2026-07-01T00:00:00+00:00")
    ack = mp.publish_as(instance, mp.BOB, [], [bad])
    assert bad["thread_hash"] not in ack["threads"]["accepted"]
    assert f"assignment from_id must be the lineage genesis, not the head {v2}" == \
        _reject_reason(ack, bad["thread_hash"])
    assert instance.store.get_thread(bad["thread_hash"]) is None

    # Positive control: the same assignment FROM THE GENESIS is admitted.
    good = _thread("assignment", g, "burr-0715", "2026-07-02T00:00:00+00:00")
    ack2 = mp.publish_as(instance, mp.BOB, [], [good])
    assert good["thread_hash"] in ack2["threads"]["accepted"], _reject_reason(
        ack2, good["thread_hash"]
    )


# ============================================================================
# S1 — SEIZE A NAME (the slug claim under ON).
#
# Naming follows OWNERSHIP OF THE ANCHOR, and the claim itself is a signer-PAIR decision
# (§3.3/§6, station_store.claim_slug): free -> 'claimed'; the SAME signer -> 're-anchored';
# a DIFFERENT signer -> 'collision' (nothing written) UNLESS an operator/administrator
# 'override's, which rewrites the anchor AND the claimant pair.
#
# Every existing collision test models party 2 as a pre-seeded claim_slug row under the
# constant-ALICE verifier (test_perm_ingress.py:109), so the collision has never been
# driven by a second signer really publishing. And the plan's named gap: "operator
# override (only administrator is tested)" — test_perm_fell_r1.py:116 covers
# administrator; operator has none. Both closed here, with the observable stated as
# WHO OWNS THE SLUG ANCHOR AFTER.
#
# GUARD STACK: the ingress gates a claim on `slug_override or owns_folio(anchor)`, then
# claim_slug re-decides on the signer pair. In (a) and (c) BOB publishes his OWN site, so
# he owns his own anchor and owns_folio PASSES — the collision/override decision inside
# claim_slug is the single load-bearing guard, which is what makes those two isolating.
# ============================================================================

def _alice_holds_specs(instance) -> str:
    """ALICE (bound originator) publishes her site and claims the name 'specs'. Returns
    her site genesis — the anchor the claim is recorded against."""
    mp.bind_party(instance, mp.ALICE, "originator")
    site = _publish_folio(instance, mp.ALICE, "alice specs", "as", _TS,
                          ftype="site", slug="specs")
    claim = instance.store.get_slug_claim("specs")
    assert claim["anchor_hash"] == site and claim["claimed_by_subject"] == mp.ALICE, claim
    return site


def _bob_claims_a_free_name_and_collides(instance) -> str:
    """BOB publishes TWO of his OWN site folios in ONE batch: one under a FREE name, one
    under ALICE's 'specs'. Returns the hash of the 'specs' contender.

    THE FREE CLAIM IS A LIVENESS CONTROL, and it is why the collision assertions below
    mean anything. Every one of them says "nothing changed" — which is also what you get
    if BOB's slug pass never ran or THREW, since the ingress wraps each claim in
    `except Exception` and only debug-logs it (ingress.py:509). The free name proves the
    pass reached claim_slug and WROTE, for this signer, in this batch, in this
    transaction. So a 'specs' claim that then changes nothing is the signer-pair decision
    refusing, not the ingress swallowing an error."""
    free = h.folio("site", "bob free site", "bf", BOB_TS)
    contender = h.folio("site", "bob specs", "bs", BOB_TS)
    ack = mp.publish_as(
        instance, mp.BOB, [free, contender], [],
        site_slugs={free["content_hash"]: "bob-notes", contender["content_hash"]: "specs"},
    )
    for f in (free, contender):
        assert f["content_hash"] in ack["accepted"], ack

    # The control: BOB's slug pass DOES reach claim_slug and write, in this very batch.
    got = instance.store.get_slug_claim("bob-notes")
    assert got is not None, "BOB's slug pass never wrote — the collision below proves nothing"
    assert got["anchor_hash"] == free["content_hash"], got
    assert got["claimed_by_subject"] == mp.BOB, got
    assert instance.store.resolve_slug("bob-notes") == free["content_hash"]
    return contender["content_hash"]


def test_s1_foreign_signer_cannot_collide_on_an_owned_name(instance):
    """(a) A DIFFERENT signer's collision is refused. BOB publishes his OWN site folio —
    so he owns his own anchor and the ingress ownership gate passes — under ALICE's name
    'specs'. The claim is a signer-pair decision, so it collides: NOTHING is written and
    the name still resolves to ALICE's lineage. The free-name control in the helper is
    what makes "nothing written" a refusal rather than a no-op."""
    alice_site = _alice_holds_specs(instance)
    mp.bind_party(instance, mp.BOB, "originator")

    bob_site = _bob_claims_a_free_name_and_collides(instance)

    # The observable: who owns the slug anchor after. BOB's folio landed; the NAME did not.
    claim = instance.store.get_slug_claim("specs")
    assert claim["anchor_hash"] == alice_site
    assert claim["claimed_by_subject"] == mp.ALICE
    assert instance.store.resolve_slug("specs") == alice_site
    assert instance.store.get_folio(bob_site) is not None  # the seizure failed, not the publish


def test_s1_same_signer_re_anchors_its_own_name(instance):
    """(b) A SAME-signer re-anchor is ALLOWED — the follow-on threat
    test_perm_ingress.py:154's own docstring names ("then same-signer re-anchors it onto
    her own lineage"), never tested. This is the DESIGNED behaviour, and it is the reason
    (a) and (c) matter: a name follows its claimant, so whoever holds the claim can move
    it. ALICE repoints 'specs' onto a brand-new, unrelated site lineage of her own."""
    first = _alice_holds_specs(instance)
    second = _publish_folio(instance, mp.ALICE, "alice specs II", "as2",
                            "2026-02-01T00:00:00+00:00", ftype="site", slug="specs")
    assert second != first

    claim = instance.store.get_slug_claim("specs")
    assert claim["anchor_hash"] == second           # re-anchored onto the NEW lineage
    assert claim["claimed_by_subject"] == mp.ALICE  # same claimant throughout
    assert instance.store.resolve_slug("specs") == second


_S1_ROLE_ARMS = [
    ("originator", False),      # the negative control: a bound non-tier cannot override
    ("steward", False),         # §6 scopes the override to administrator/operator ONLY
    ("operator", True),         # the plan's gap: only administrator was tested
    ("administrator", True),    # the covered case, kept as the operator's twin
]


def test_s1_role_arms_cover_every_station_role():
    """The arms below are hand-enumerated, so pin them to the roles the station actually
    binds: a new tier added to STATION_ROLES fails here instead of quietly acquiring an
    untested naming right (and a removed one fails here instead of testing a dead arm)."""
    assert {role for role, _ in _S1_ROLE_ARMS} == set(STATION_ROLES)


@pytest.mark.parametrize("role,seizes", _S1_ROLE_ARMS)
def test_s1_only_operator_or_administrator_overrides_a_name(instance, role, seizes):
    """(c) The OPERATOR override, and the boundary around it. A tiered signer colliding on
    ALICE's name either takes it outright (operator/administrator) or is refused exactly
    like any other foreign signer. Isolating: BOB publishes his OWN site in every arm, so
    the ONLY variable is his role — the ingress `slug_override` read and claim_slug's
    override branch are the single load-bearing guard.

    The steward arm is the sharp one: a steward CAN moderate any lineage
    (_TIER_MAY_ACT) yet must NOT be able to rename one — moderation power and naming
    power are deliberately different rights (§6). The refusing arms carry the same
    free-name liveness control as (a), so "nothing written" is a refusal by role and not
    a slug pass that threw for this role."""
    alice_site = _alice_holds_specs(instance)
    mp.bind_party(instance, mp.BOB, role)

    bob_site = _bob_claims_a_free_name_and_collides(instance)

    claim = instance.store.get_slug_claim("specs")
    if seizes:
        # The anchor AND the claimant pair are rewritten — BOB now holds the name.
        assert claim["anchor_hash"] == bob_site
        assert claim["claimed_by_subject"] == mp.BOB
        assert instance.store.resolve_slug("specs") == bob_site
    else:
        assert claim["anchor_hash"] == alice_site
        assert claim["claimed_by_subject"] == mp.ALICE
        assert instance.store.resolve_slug("specs") == alice_site


def test_s1_override_does_not_leak_into_the_thread_authorization(instance):
    """The override is scoped to NAMING. An operator seizing a name gains no from-end
    power: the same operator still cannot originate a thread from a folio it does not
    hold (owns_folio, §5.1). Pins the two rights apart so a future widening of
    `slug_override` cannot quietly widen the write model with it."""
    alice_site = _alice_holds_specs(instance)
    mp.bind_party(instance, mp.BOB, "operator")
    _publish_folio(instance, mp.BOB, "bob op specs", "bs", BOB_TS, ftype="site", slug="specs")
    assert instance.store.get_slug_claim("specs")["claimed_by_subject"] == mp.BOB  # seized

    # ...but ALICE's site folio is still not an origin BOB may publish from.
    member = _publish_folio(instance, mp.BOB, "op member", "m", "2026-06-02T00:00:00+00:00")
    bad = _thread("reference", alice_site, member)
    ack = mp.publish_as(instance, mp.BOB, [], [bad])
    assert bad["thread_hash"] not in ack["threads"]["accepted"]
    assert "cannot originate from a folio you do not hold" in _reject_reason(
        ack, bad["thread_hash"]
    )


# ============================================================================
# S6 — CAPTURE A LEGACY LINEAGE (the unresolved-genesis contract).
#
# lineage_genesis_for returns Genesis(resolved=False) on a LEGACY GAP: the chain's oldest
# HELD node still carries a supersedes edge to an UNHELD parent, and that child is
# UNSIGNED (no covering manifest), so the gap is import incompleteness rather than
# forgery. On such a genesis the anchor is indeterminate — may_act_on_lineage skips BOTH
# the owner check and the grant check, leaving ONLY a station tier able to act (§4).
#
# The plan: "the unresolved-genesis contract, which no test reaches at all" — deleting
# both `if genesis.resolved:` guards leaves all 130 tests green, and an instrumented run
# found the mutant never diverged across 42 calls. These tests reach it.
#
# WHICH GUARD IS KILLABLE — a correction to the plan's "both" (see the S6 report):
#   - The GRANT guard (the second) is genuinely load-bearing and
#     test_s6_a_grant_never_reaches_an_unresolved_genesis KILLS its mutant: with it
#     removed, BOB's grant satisfies the to-end and the attack is ADMITTED.
#   - The OWNER guard (the first) is an EQUIVALENT MUTANT for every genesis the system
#     can actually present, provably: Genesis(resolved=False) is RETURNED at exactly one
#     site (thread_authz.py:295), on the branch where `store.get_constituent_proof(node)
#     is None` — and lineage_owner is _proof_signer over THAT SAME proof, so it returns
#     None for such an anchor no matter what. Removing the guard cannot change an outcome
#     for any genesis lineage_genesis_for can produce, which is every genesis reaching
#     may_act_on_lineage from authorize_thread (all four call sites resolve through
#     lineage_genesis_for / _resolve_or_reject; nothing constructs a Genesis by hand).
#     The scope matters: a HAND-BUILT Genesis(some_owned_hash, resolved=False) WOULD
#     diverge under the mutant — the guard is what makes it not diverge — but the
#     resolver never emits that pairing, so no test can reach it through the real path.
#     The guard is defense in depth against a future second construction site, and
#     test_s6_an_unresolved_genesis_confers_no_ownership_right pins the property it
#     exists to protect rather than pretending to kill an unkillable mutant.
# ============================================================================

def _legacy_gapped_lineage(instance) -> str:
    """A genuinely UNRESOLVED genesis, built the way one really arises: a pre-ON (legacy)
    import. Under the OFF posture the ingress is manifest-blind, so the folio lands with
    NO covering-manifest attribution; its supersedes edge points at a predecessor the
    station never received. Walking back from it therefore hits an UNHELD parent below an
    UNSIGNED child — the legacy gap, Genesis(resolved=False).

    Returns the held legacy head, which IS the unresolved anchor."""
    legacy = h.folio("finding", "legacy import", "L", "2020-01-01T00:00:00+00:00")
    node = legacy["content_hash"]
    missing_parent = "sha256::" + "f" * 64          # never held here
    gap = _thread("supersedes", node, missing_parent, "2020-01-02T00:00:00+00:00")
    ack = mp.publish_as(instance, "legacy@x", [legacy], [gap], require_signed=False)
    assert node in ack["accepted"], ack
    assert gap["thread_hash"] in ack["threads"]["accepted"], ack["threads"]

    # The precondition this whole scenario rests on, asserted rather than assumed: the
    # authorization path really does see an UNRESOLVED genesis here (an unsigned child
    # over an unheld parent), not a resolved one and not a LineageReject.
    genesis = lineage_genesis_for(instance.store, node)
    assert genesis == Genesis(node, resolved=False), genesis
    assert instance.store.get_constituent_proof(node) is None  # legacy: no attribution
    return node


def test_s6_the_fixture_really_is_an_unresolved_genesis(instance):
    """Guards the scenario against silently degenerating into a resolved-genesis test —
    the way the plan says every previous attempt at this contract failed (the mutant
    "never diverged once across 42 calls"). If a future change makes the OFF path write
    attribution, or makes a dangling parent reject outright, THIS fails first and the
    three tests below stop being about something they no longer reach."""
    node = _legacy_gapped_lineage(instance)
    g = lineage_genesis_for(instance.store, node)
    assert g.resolved is False
    assert g.hash == node
    # And the two predicates the contract turns on, read directly at rung 0.
    assert lineage_owner(instance.store, g.hash) is None
    mp.bind_party(instance, mp.BOB, "originator")
    mp.grant_on(instance, node, mp.BOB, "supersede")
    assert instance.store.has_active_grant(node, h.I, mp.BOB, "supersede")  # the grant IS live
    # The Principal is built from the SAME (issuer, subject) the grant was written under,
    # locally — an imported one declaring its own issuer could drift from h.I, and then
    # may_act_on_lineage would return False because the pair matches NOTHING, making this
    # assertion pass vacuously instead of proving the resolved-guard skipped a live grant.
    bob = Principal(h.I, mp.BOB)
    assert may_act_on_lineage(instance.store, bob, g, "supersede") is False  # ...and skipped


def test_s6_a_grant_never_reaches_an_unresolved_genesis(instance):
    """(a) THE MUTANT-KILLER. BOB holds a LIVE supersede grant at the legacy anchor and is
    a bound originator (no tier). On a RESOLVED genesis that grant admits him — the
    positive control below proves it. On the UNRESOLVED one it is SKIPPED, because the
    anchor is indeterminate and a grant cannot attach to an anchor the station cannot
    name (§4). Delete the second `if genesis.resolved:` in may_act_on_lineage and this
    attack is ADMITTED."""
    node = _legacy_gapped_lineage(instance)
    mp.bind_party(instance, mp.BOB, "originator")
    mp.grant_on(instance, node, mp.BOB, "supersede")   # a live grant AT the legacy anchor

    bob_v = h.folio("finding", "capture attempt", "c", BOB_TS)
    edge = _thread("supersedes", bob_v["content_hash"], node)
    ack = mp.publish_as(instance, mp.BOB, [bob_v], [edge])

    assert edge["thread_hash"] not in ack["threads"]["accepted"], ack["threads"]
    assert f"no edit access to {node}; fork instead" == _reject_reason(ack, edge["thread_hash"])
    # Observable (claim 1): the legacy lineage is uncaptured — no supersedes onto it, and
    # it is still owner-None, so a later republish cannot inherit anything from this try.
    assert not instance.store.get_threads(to_id=node, type="supersedes")
    assert lineage_owner(instance.store, node) is None

    # POSITIVE CONTROL — the SAME grant kind, the same signer, the same edge shape, on a
    # RESOLVED genesis: admitted. So it is the UNRESOLVEDNESS that refuses above, not a
    # broken grant, an unbound signer, or a malformed edge.
    mp.bind_party(instance, mp.ALICE, "originator")
    ok_anchor = _publish_folio(instance, mp.ALICE, "resolved anchor", "r", _TS)
    mp.grant_on(instance, ok_anchor, mp.BOB, "supersede")
    bob_v2 = h.folio("finding", "grant works here", "c2", "2026-06-02T00:00:00+00:00")
    good = _thread("supersedes", bob_v2["content_hash"], ok_anchor,
                   "2026-06-02T00:00:00+00:00")
    ack2 = mp.publish_as(instance, mp.BOB, [bob_v2], [good])
    assert good["thread_hash"] in ack2["threads"]["accepted"], _reject_reason(
        ack2, good["thread_hash"]
    )


def test_s6_an_unresolved_genesis_confers_no_ownership_right(instance):
    """(b) The owner-None path grants NOTHING. A legacy anchor has no covering-manifest
    signer, so there is no owner for any signer to match: lineage_owner returns None and
    may_act_on_lineage's owner arm is `owner is not None and owner == (issuer, subject)`.
    (§4's "never a None==None match" is a note on that shape, not a testable behaviour —
    a Principal always carries a real issuer/subject pair, and None never equals a tuple,
    so no input reaches such a comparison. What IS testable is the property below.)

    BOB re-publishing the very same legacy folio bytes does NOT make him its owner (the
    ingress refuses to first-attribute a pre-existing unattributed folio, fell-r1
    finding 1), so he cannot convert a legacy lineage into one he owns and then
    supersede it."""
    node = _legacy_gapped_lineage(instance)
    mp.bind_party(instance, mp.BOB, "originator")

    # BOB re-publishes the identical legacy folio under his own signature.
    legacy = h.folio("finding", "legacy import", "L", "2020-01-01T00:00:00+00:00")
    assert legacy["content_hash"] == node
    ack0 = mp.publish_as(instance, mp.BOB, [legacy], [])
    assert node in ack0["existing"], ack0        # the folio is already held...
    assert lineage_owner(instance.store, node) is None   # ...and STILL owner-None

    # So the supersede is still refused, and by the to-end right — not by shape.
    bob_v = h.folio("finding", "post-republish attempt", "p", BOB_TS)
    edge = _thread("supersedes", bob_v["content_hash"], node)
    ack = mp.publish_as(instance, mp.BOB, [bob_v], [edge])
    assert edge["thread_hash"] not in ack["threads"]["accepted"]
    assert f"no edit access to {node}; fork instead" == _reject_reason(ack, edge["thread_hash"])


@pytest.mark.parametrize("role,admits", [
    ("originator", False),      # the negative control — the tier is what admits, below
    ("steward", True),
    ("administrator", True),
    ("operator", True),
])
def test_s6_only_a_station_tier_may_act_on_an_unresolved_genesis(instance, role, admits):
    """(c) A station tier CAN act — the legacy-moderation path (§4) that makes the
    unresolved-genesis degradation safe rather than a dead end. This is the liveness side
    (claim 2): if the tier arm failed, an owner-None legacy lineage would be permanently
    unmaintainable by anyone, which is the bricking shape S7 exists to refuse."""
    node = _legacy_gapped_lineage(instance)
    mp.bind_party(instance, mp.BOB, role)

    bob_v = h.folio("finding", f"{role} moderation", "mod", BOB_TS)
    edge = _thread("supersedes", bob_v["content_hash"], node)
    ack = mp.publish_as(instance, mp.BOB, [bob_v], [edge])

    if admits:
        assert edge["thread_hash"] in ack["threads"]["accepted"], _reject_reason(
            ack, edge["thread_hash"]
        )
        assert instance.store.get_thread(edge["thread_hash"]) is not None
    else:
        assert edge["thread_hash"] not in ack["threads"]["accepted"]
        assert f"no edit access to {node}; fork instead" == _reject_reason(
            ack, edge["thread_hash"]
        )
