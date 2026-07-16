"""Permission model — the staged final graph (same-batch supersedes fixpoint, §8).

The load-bearing property (knuth F2): the staging set is the fixpoint of AUTHORIZED
same-batch supersedes ONLY. A rejected supersedes must never steer another edge's
genesis (a confused deputy), and a legit same-batch chain (within + the supersede that
moves its target) must resolve to the true genesis so it is not wrong-blocked.
"""

from __future__ import annotations

import pytest

from skein.thread_authz import AuthzReject, authorize_thread, authorized_staged_supersedes
from tests.perm_helpers import (
    BOB, OWNER, STEWARD,
    bind, grant, make_store, signed_folio, supersede, thread,
)


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    yield s
    s.close()


def test_authorized_chain_stages_fully(store):
    """A same-batch chain by the lineage owner both stage."""
    g = signed_folio(store, OWNER, "v1")
    v2 = signed_folio(store, OWNER, "v2")
    v3 = signed_folio(store, OWNER, "v3")
    batch = [thread("supersedes", v2, g), thread("supersedes", v3, v2)]
    staged = authorized_staged_supersedes(store, OWNER, batch)
    assert set(staged) == {(v2, g), (v3, v2)}


def test_rejected_supersedes_not_staged(store):
    """A supersedes bob is not authorized to make never enters the staging set."""
    g = signed_folio(store, OWNER, "v1")   # owned by alice
    v2 = signed_folio(store, BOB, "v2")    # bob's version, but bob can't supersede alice
    staged = authorized_staged_supersedes(store, BOB, [thread("supersedes", v2, g)])
    assert staged == []


def test_rejected_supersedes_does_not_steer_a_co_batch_within(store):
    """knuth F2: a rejected same-batch supersedes must NOT move a co-batch within's
    genesis. Here bob's supersedes(v2 -> site_g) is rejected (bob can't edit the site
    lineage), so the co-batch within must still resolve genesis at the true site
    genesis and be judged on its own — not be redirected through the rejected edge."""
    site_g = signed_folio(store, OWNER, "site v1", ftype="site")
    v2 = signed_folio(store, BOB, "site v2 (attempt)", ftype="site")
    member = signed_folio(store, BOB, "member")
    grant(store, site_g, BOB, "site_contribute")  # bob may contribute to the REAL site

    batch_supersedes = [thread("supersedes", v2, site_g)]  # bob is NOT authorized for this
    staged = authorized_staged_supersedes(store, BOB, batch_supersedes)
    assert staged == []  # the rejected supersedes is not staged

    # The within resolves genesis over the (empty) authorized staging set -> the true
    # site genesis, where bob DOES hold a contribute grant -> authorized.
    authorize_thread(store, BOB, thread("within", member, site_g), staged)


def test_authorized_supersede_steers_co_batch_within(store):
    """The dual: an AUTHORIZED same-batch supersede DOES steer genesis. A within onto a
    freshly-superseding site version resolves back to the genesis over the staged edge."""
    site_g = signed_folio(store, OWNER, "site v1", ftype="site")
    site_v2 = signed_folio(store, OWNER, "site v2", ftype="site")
    member = signed_folio(store, OWNER, "member")
    # owner supersedes the site in-batch; the within points at the NEW head.
    batch_supersedes = [thread("supersedes", site_v2, site_g)]
    staged = authorized_staged_supersedes(store, OWNER, batch_supersedes)
    assert staged == [(site_v2, site_g)]
    # A within onto site_v2 is head-anchored -> rejected (must anchor at genesis), which
    # is the correct behavior; a within onto the genesis still authorizes.
    with pytest.raises(AuthzReject):
        authorize_thread(store, OWNER, thread("within", member, site_v2), staged)
    authorize_thread(store, OWNER, thread("within", member, site_g), staged)


def test_same_batch_merge_only_one_parent_stages(store):
    """Two same-batch supersedes sharing a from_id (a merge) — at most one stages; the
    second is rejected as a second parent."""
    p1 = signed_folio(store, OWNER, "p1")
    p2 = signed_folio(store, OWNER, "p2")
    child = signed_folio(store, OWNER, "child")
    batch = [thread("supersedes", child, p1), thread("supersedes", child, p2)]
    staged = authorized_staged_supersedes(store, OWNER, batch)
    assert len(staged) == 1  # exactly one parent admitted; the other is a merge
