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
    bind, grant, make_store, signed_folio, supersede, thread,
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


# --- attribution ------------------------------------------------------------


def test_attribution_owner_ok_nonowner_rejected(store):
    doc = signed_folio(store, OWNER, "doc")
    _ok(store, OWNER, thread("attribution", doc, "burr-0715"))  # to-end is an agent
    reason = _reject(store, BOB, thread("attribution", doc, "burr-0715"))
    assert reason == "cannot originate from a folio you do not hold"


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
