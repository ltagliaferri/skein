"""Permission model — authorize_thread per-class matrix (rev 6 §5.1, brief-20260716-u73x).

The enforcement core. Two distinct predicates gate the two ends: owns_folio (from-end,
pure, never grant-satisfiable) and may_act_on_lineage (to-end, owner|tier|grant). Every
class is exercised for both the ACCEPT and the fail-closed REJECT direction.
"""

from __future__ import annotations

import pytest

from skein.thread_authz import AuthzReject, authorize_thread
from tests.perm_helpers import (
    ADMIN, BOB, OWNER, STEWARD,
    bind, grant, make_store, sign, signed_folio, supersede, thread,
)


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    yield s
    s.close()


def _ok(store, signer, th, pending=()):
    authorize_thread(store, signer, th, pending)  # raises on reject


def _reject(store, signer, th, pending=()):
    with pytest.raises(AuthzReject) as ei:
        authorize_thread(store, signer, th, pending)
    return ei.value.reason


# --- supersedes (AFFECTING) -------------------------------------------------


def test_owner_supersedes_own_lineage(store):
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, OWNER, "v2")
    _ok(store, OWNER, thread("supersedes", v2, g))


def test_nonowner_supersedes_rejected_no_edit_access(store):
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, BOB, "v2")  # bob's own new version
    reason = _reject(store, BOB, thread("supersedes", v2, g))
    assert "no edit access" in reason and "fork" in reason


def test_steward_may_supersede_any_lineage(store):
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, STEWARD, "v2")
    bind(store, STEWARD, "steward")
    _ok(store, STEWARD, thread("supersedes", v2, g))


def test_administrator_may_supersede_any_lineage(store):
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, ADMIN, "v2")
    bind(store, ADMIN, "administrator")
    _ok(store, ADMIN, thread("supersedes", v2, g))


def test_grantee_with_supersede_grant_may_supersede(store):
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, BOB, "v2")
    grant(store, g, BOB, "supersede")  # grant at the genesis anchor
    _ok(store, BOB, thread("supersedes", v2, g))


def test_revoked_grant_no_longer_authorizes(store):
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, BOB, "v2")
    grant(store, g, BOB, "supersede")
    store.revoke_grant(g, BOB.issuer, BOB.subject, "supersede")
    _reject(store, BOB, thread("supersedes", v2, g))


def test_site_contribute_grant_does_not_authorize_supersede(store):
    """A grant is kind-scoped: a site_contribute grant never satisfies a supersede."""
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, BOB, "v2")
    grant(store, g, BOB, "site_contribute")
    _reject(store, BOB, thread("supersedes", v2, g))


def test_supersede_from_end_forgery_rejected(store):
    """A signer superseding with a NEW version folio they do not own (first-wins
    attribution belongs to someone else) fails the pure from-end ownership."""
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, OWNER, "v2")  # attributed to alice
    reason = _reject(store, BOB, thread("supersedes", v2, g))  # bob signs the edge
    assert reason == "cannot originate from a folio you do not hold"


def test_supersede_dangling_target_rejected(store):
    v2 = signed_folio(store, OWNER, "v2")
    reason = _reject(store, OWNER, thread("supersedes", v2, "sha256::" + "d" * 64))
    assert "not held" in reason


def test_supersede_alias_target_rejected(store):
    v2 = signed_folio(store, OWNER, "v2")
    reason = _reject(store, OWNER, thread("supersedes", v2, "finding-20260101-abcd"))
    assert "content hash" in reason


def test_grant_never_satisfies_the_from_end(store):
    """Even with a supersede grant, an UNOWNED from_id origin still fails closed — a
    grant satisfies the to-end only (§5.1)."""
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, OWNER, "v2")  # attributed to alice
    grant(store, g, BOB, "supersede")
    reason = _reject(store, BOB, thread("supersedes", v2, g))
    assert reason == "cannot originate from a folio you do not hold"


# --- within (AFFECTING) -----------------------------------------------------


def test_owner_files_own_folio_into_own_site(store):
    site = signed_folio(store, OWNER, "site", ftype="site")
    member = signed_folio(store, OWNER, "member")
    _ok(store, OWNER, thread("within", member, site))


def test_nonowner_contribute_rejected(store):
    site = signed_folio(store, OWNER, "site", ftype="site")
    member = signed_folio(store, BOB, "member")  # bob owns his member folio
    reason = _reject(store, BOB, thread("within", member, site))
    assert "no contribute access" in reason


def test_site_contribute_grant_authorizes_within(store):
    site = signed_folio(store, OWNER, "site", ftype="site")
    member = signed_folio(store, BOB, "member")
    grant(store, site, BOB, "site_contribute")
    _ok(store, BOB, thread("within", member, site))


def test_site_edit_grant_also_authorizes_within(store):
    """site_edit is the broader site power — it satisfies a site_contribute request."""
    site = signed_folio(store, OWNER, "site", ftype="site")
    member = signed_folio(store, BOB, "member")
    grant(store, site, BOB, "site_edit")
    _ok(store, BOB, thread("within", member, site))


def test_within_filing_someone_elses_folio_rejected(store):
    """The breadcrumb-hijack close (§5.5): the member folio's OWN ownership is required
    (pure from-end), NOT grant-satisfiable — even a site_contribute grantee cannot file
    a victim's folio into the site."""
    site = signed_folio(store, OWNER, "site", ftype="site")
    victim = signed_folio(store, OWNER, "victim")  # owned by alice
    grant(store, site, BOB, "site_contribute")
    reason = _reject(store, BOB, thread("within", victim, site))
    assert reason == "cannot originate from a folio you do not hold"


def test_head_anchored_within_rejected(store):
    """within must anchor at the site GENESIS, not a superseded site head (§5.2)."""
    site_g = signed_folio(store, OWNER, "site v1", ftype="site")
    site_v2 = signed_folio(store, OWNER, "site v2", ftype="site")
    supersede(store, site_v2, site_g)  # site_v2 is now the head
    member = signed_folio(store, OWNER, "member")
    reason = _reject(store, OWNER, thread("within", member, site_v2))
    assert "genesis" in reason


# --- pointer (reference/reply/mention/forks/responds_to/imports_legacy/tag) --


@pytest.mark.parametrize("ptype", ["reference", "reply", "mention", "forks", "responds_to", "imports_legacy", "tag"])
def test_pointer_owner_ok_nonowner_from_rejected(store, ptype):
    owned = signed_folio(store, OWNER, "owned")
    target = signed_folio(store, OWNER, "target")
    _ok(store, OWNER, thread(ptype, owned, target))         # from owned, no to-end right
    # a from-end the signer does not own fails closed
    reason = _reject(store, BOB, thread(ptype, owned, target))
    assert reason == "cannot originate from a folio you do not hold"


def test_pointer_dangling_target_allowed(store):
    owned = signed_folio(store, OWNER, "owned")
    _ok(store, OWNER, thread("reference", owned, "sha256::" + "e" * 64))  # unheld to-end OK


def test_pointer_alias_target_rejected(store):
    """fell-r3: a pointer to-end must be a content hash — an ALIAS/legacy-id to-end is
    rejected on the signed path (§5.1), else it resolves through the mutable aliases
    table to a target the signature never fixed."""
    owned = signed_folio(store, OWNER, "owned")
    reason = _reject(store, OWNER, thread("reference", owned, "finding-20260101-alias"))
    assert "content hash" in reason


def test_supersedes_cycle_rejected(store):
    """fell-r3: a supersedes that would CLOSE A CYCLE (B->A when A->B is staged) is
    rejected here, before it can be stored/staged and poison a co-batch edge's genesis."""
    a = signed_folio(store, OWNER, "a")
    b = signed_folio(store, OWNER, "b")
    # A->B is staged; B->A would close the cycle
    _reject(store, OWNER, thread("supersedes", b, a), pending=[(a, b)])


# --- control: status / reverted / archive -----------------------------------


def test_status_owner_ok_nonowner_rejected(store):
    g = signed_folio(store, OWNER, "doc")
    _ok(store, OWNER, thread("status", g, g))              # self-loop, owner
    reason = _reject(store, BOB, thread("status", g, g))   # bob is nobody here
    assert "no moderation access" in reason


def test_status_steward_moderation_from_end_exempt(store):
    """CONTROL from-end is exempt — a steward sets status on a folio they do not own."""
    g = signed_folio(store, OWNER, "doc")
    bind(store, STEWARD, "steward")
    _ok(store, STEWARD, thread("status", g, g))


def test_reverted_same_lineage_ok(store):
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, OWNER, "v2")
    supersede(store, v2, g)  # v2 is head, g is ancestor
    _ok(store, OWNER, thread("reverted", v2, g))


def test_reverted_cross_lineage_rejected(store):
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, OWNER, "v2")
    supersede(store, v2, g)
    other = signed_folio(store, OWNER, "unrelated")  # different lineage
    reason = _reject(store, OWNER, thread("reverted", v2, other))
    assert "ancestor" in reason or "lineage" in reason


def test_reverted_from_non_head_rejected(store):
    """fell-r3: reverted's from_id must be the CURRENT HEAD (§5.4) — a revert from an
    already-superseded version is malformed."""
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, OWNER, "v2")
    v3 = signed_folio(store, OWNER, "v3")
    supersede(store, v2, g)
    supersede(store, v3, v2)  # v3 is the head; v2 is superseded
    reason = _reject(store, OWNER, thread("reverted", v2, g))  # from a non-head
    assert "head" in reason
    _ok(store, OWNER, thread("reverted", v3, g))  # from the head, to an ancestor: fine


def test_reverted_fork_sibling_rejected(store):
    """fell-r1 finding 4: a fork sibling shares the genesis but is NOT a reachable
    ancestor of the head — reverting to it must fail closed."""
    g = signed_folio(store, OWNER, "v1")
    a = signed_folio(store, OWNER, "childA")
    b = signed_folio(store, OWNER, "childB")
    supersede(store, a, g)  # a supersedes g
    supersede(store, b, g)  # b ALSO supersedes g (fork — a and b are siblings)
    # reverted(from=a, to=b): same genesis, but b is not an ancestor of a
    reason = _reject(store, OWNER, thread("reverted", a, b))
    assert "ancestor" in reason
    # reverting to the true ancestor g is fine
    _ok(store, OWNER, thread("reverted", a, g))


def test_status_agent_origin_rejected(store):
    """fell-r1 finding 5: a status edge with a non-self-loop (agent-origin) from_id must
    be rejected — status is a self-loop from=to=genesis, and no agent origin travels."""
    g = signed_folio(store, OWNER, "doc")
    reason = _reject(store, OWNER, thread("status", "burr-0715", g))
    assert "self-loop" in reason
    # the proper self-loop by the owner is accepted
    _ok(store, OWNER, thread("status", g, g))


def test_status_on_non_genesis_head_rejected(store):
    """fell-r2: status must anchor at the GENESIS, not a non-genesis head — a self-loop
    on a superseding version is rejected so control state stays single-valued."""
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, OWNER, "v2")
    supersede(store, v2, g)  # v2 is a head, g is the genesis
    reason = _reject(store, OWNER, thread("status", v2, v2))  # self-loop on the head
    assert "genesis" in reason
    _ok(store, OWNER, thread("status", g, g))  # self-loop on the genesis is fine


def test_archive_self_loop_on_genesis_enforced(store):
    """fell-r2: archive is a self-loop on the genesis like status — a two-folio archive,
    an agent-origin from_id, and a self-loop on a non-genesis head all reject."""
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, OWNER, "v2")
    supersede(store, v2, g)
    bind(store, STEWARD, "steward")
    assert "self-loop" in _reject(store, STEWARD, thread("archive", g, v2))       # from!=to
    assert "self-loop" in _reject(store, STEWARD, thread("archive", "burr-0715", g))  # agent origin
    assert "genesis" in _reject(store, STEWARD, thread("archive", v2, v2))        # on the head
    _ok(store, STEWARD, thread("archive", g, g))  # self-loop on the genesis is fine


def test_proof_missing_attribution_confers_no_ownership(store):
    """audit-cap: an attribution row whose manifest parent is GONE (proof_missing) must
    confer NO ownership — the read path already refuses to verify such a folio
    ('NOT VERIFIED — proof missing'), so trusting its denormalized signer on the WRITE
    path would be a fail-open the read side rejects. Only a tier may act."""
    g = signed_folio(store, OWNER, "doc")
    v2 = signed_folio(store, OWNER, "v2")
    # orphan the attribution: drop its covering manifest (FK off — we are simulating the
    # corrupt/mid-migration state get_constituent_proof reports as proof_missing)
    store.conn.execute("PRAGMA foreign_keys=OFF")
    store.conn.execute("DELETE FROM manifests")
    proof = store.get_constituent_proof(g)
    assert proof is not None and proof["proof_missing"] is True  # the corrupt state
    assert proof["subject"] == OWNER.subject  # OWNER is still NAMED in the stale row...
    # ...but confers NO ownership on either predicate
    from skein.thread_authz import lineage_owner, owns_folio
    assert owns_folio(store, OWNER, g) is False      # from-end: pure ownership
    assert lineage_owner(store, g) is None           # to-end: owner-of-genesis
    # so both a supersedes onto that lineage and a pointer from it fail closed
    _reject(store, OWNER, thread("supersedes", v2, g))
    assert _reject(store, OWNER, thread("reference", g, g)) == (
        "cannot originate from a folio you do not hold"
    )


def test_pointer_from_a_thread_hash_rejected(store):
    """fell-r2: owns_folio requires a HELD FOLIO from-end — a thread hash the signer
    published must NOT satisfy a folio from-end (attribution is keyed by hash regardless
    of kind)."""
    target = signed_folio(store, OWNER, "target")
    # attribute a THREAD hash to OWNER (as the ingress would for a published thread)
    fake_thread_hash = "sha256::" + "7" * 64
    sign(store, fake_thread_hash, OWNER, kind="thread")
    reason = _reject(store, OWNER, thread("reference", fake_thread_hash, target))
    assert reason == "cannot originate from a folio you do not hold"


# --- assignment -------------------------------------------------------------


def test_assignment_steward_ok_no_from_ownership(store):
    """assignment: no from-end ownership (a steward assigns another's folio), to-end is
    an agent (no resolve), gate on supersession at the from-folio's genesis."""
    doc = signed_folio(store, OWNER, "doc")
    bind(store, STEWARD, "steward")
    _ok(store, STEWARD, thread("assignment", doc, "burr-0715"))  # to-end is an agent id


def test_assignment_nonprivileged_rejected(store):
    doc = signed_folio(store, OWNER, "doc")
    reason = _reject(store, BOB, thread("assignment", doc, "burr-0715"))
    assert "no assignment access" in reason


def test_assignment_owner_ok(store):
    doc = signed_folio(store, OWNER, "doc")
    _ok(store, OWNER, thread("assignment", doc, "burr-0715"))


def test_assignment_from_non_genesis_head_rejected(store):
    """fell-r3: assignment's from_id must be the lineage GENESIS (§5.1), not a head — a
    genesis-keyed assignment reducer keys on the anchor the design mandates."""
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, OWNER, "v2")
    supersede(store, v2, g)  # v2 is the head, g the genesis
    bind(store, STEWARD, "steward")
    reason = _reject(store, STEWARD, thread("assignment", v2, "burr-0715"))
    assert "genesis" in reason
    _ok(store, STEWARD, thread("assignment", g, "burr-0715"))  # from the genesis is fine


def test_assignment_to_held_folio_rejected(store):
    """§5.4 to-end close: an assignment names an AGENT, never a held document. A to-end
    that resolves to a HELD victim folio is a forged breadcrumb — it would render on the
    victim's threads_in with an attacker-chosen title/href — even when the from-end is a
    folio the signer legitimately owns. Reject on the held-folio to-end."""
    doc = signed_folio(store, OWNER, "doc")      # a folio the signer owns (from-end OK)
    victim = signed_folio(store, BOB, "victim")  # a HELD folio owned by someone else
    reason = _reject(store, OWNER, thread("assignment", doc, victim))
    assert reason == "assignment to-end must name an agent, not a held folio"


def test_assignment_to_agent_id_still_admits(store):
    """The permissive horn (Patrick, 2026-07-17): any agent-id to-end still admits — an
    agent id is never a 64-hex content hash, so get_folio(to) is None. No content-hash
    requirement on the to-end."""
    doc = signed_folio(store, OWNER, "doc")
    _ok(store, OWNER, thread("assignment", doc, "egaragoras"))  # a non-folio agent id


# --- attribution ------------------------------------------------------------


def test_attribution_owner_ok_nonowner_rejected(store):
    doc = signed_folio(store, OWNER, "doc")
    _ok(store, OWNER, thread("attribution", doc, "burr-0715"))  # to-end is an agent
    reason = _reject(store, BOB, thread("attribution", doc, "burr-0715"))
    assert reason == "cannot originate from a folio you do not hold"


def test_attribution_to_held_folio_rejected(store):
    """§5.4 to-end close: an attribution names an AGENT, never a held document — a to-end
    that resolves to a HELD victim folio is the same forged breadcrumb as the assignment
    case, even from a folio the signer owns."""
    doc = signed_folio(store, OWNER, "doc")      # a folio the signer owns (from-end OK)
    victim = signed_folio(store, BOB, "victim")  # a HELD folio owned by someone else
    reason = _reject(store, OWNER, thread("attribution", doc, victim))
    assert reason == "attribution to-end must name an agent, not a held folio"


def test_attribution_to_agent_id_still_admits(store):
    """The permissive horn: a ::-framed agent-id to-end still admits — not a held folio,
    and no content-hash requirement on the attribution to-end."""
    doc = signed_folio(store, OWNER, "doc")
    _ok(store, OWNER, thread("attribution", doc, "agent::x"))


# --- non-folio: message / succession ----------------------------------------


def test_message_from_held_owned_folio_ok(store):
    doc = signed_folio(store, OWNER, "doc")
    target = signed_folio(store, OWNER, "target")
    _ok(store, OWNER, thread("message", doc, target))


def test_message_agent_origin_rejected(store):
    target = signed_folio(store, OWNER, "target")
    reason = _reject(store, OWNER, thread("message", "burr-0715", target))
    assert "agent-origin" in reason


def test_succession_agent_origin_rejected(store):
    target = signed_folio(store, OWNER, "target")
    reason = _reject(store, OWNER, thread("succession", "some-agent", target))
    assert "agent-origin" in reason


# --- wire-reject + unknown --------------------------------------------------


def test_published_edge_does_not_travel(store):
    doc = signed_folio(store, OWNER, "doc")
    reason = _reject(store, OWNER, thread("published", doc, "https://interskein.com"))
    assert "does not travel" in reason


def test_unknown_type_rejected(store):
    doc = signed_folio(store, OWNER, "doc")
    reason = _reject(store, OWNER, thread("brand_new_type", doc, doc))
    assert "unknown thread type" in reason


# --- tier is read LIVE (revocation flips the verdict) -----------------------


def test_revoked_steward_cannot_moderate(store):
    g = signed_folio(store, OWNER, "doc")
    bind(store, STEWARD, "steward")
    store.revoke_binding(STEWARD.issuer, STEWARD.subject)
    _reject(store, STEWARD, thread("status", g, g))
