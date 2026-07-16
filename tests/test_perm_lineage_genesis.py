"""Permission model — lineage_genesis_for: the backward supersedes walk to a single
genesis, fail-closed on merge/cycle/self-edge/signed-gap (rev 6 §4, brief-20260716-u73x).

This is the resolver the to-end right keys on. The load-bearing property (knuth +
assayer cap) is that it is SINGLE-VALUED or a REJECT — a merge is the only thing that
makes it multi-valued, and a merge, a cycle, a self-edge, and a signed gap all fail
closed. A legacy gap (unsigned child, unheld parent) degrades to unresolved so the
steward+ moderation path stays reachable.
"""

from __future__ import annotations

import pytest

from skein.station_store import StationStore
from skein.thread_authz import Genesis, LineageReject, lineage_genesis_for


I = "https://accounts.google.com"


@pytest.fixture
def store(tmp_path):
    s = StationStore(tmp_path / ".skein-station")
    yield s
    s.close()


def _folio(store, title: str, *, content: str = "body") -> str:
    """Create a held version, return its content hash."""
    return store.create_folio(
        {
            "type": "finding",
            "title": title,
            "content": content,
            "created_at": "2026-07-16T00:00:00+00:00",
            "created_by": "tester",
        }
    )


def _supersede(store, new: str, old: str) -> None:
    """Store a supersedes edge new -> old (from=new, to=old)."""
    store.save_thread(
        from_id=new, to_id=old, type="supersedes",
        created_at="2026-07-16T00:00:00+00:00",
    )


def _sign(store, content_hash: str, subject: str = "alice@example.com") -> None:
    """Attach a covering-manifest attribution so the constituent reads SIGNED (the
    manifest row is inserted first — attribution FK-references it)."""
    root = "sha256::" + "a" * 64
    store.add_manifest(root, "sha256::" + "b" * 64, "{}", "[]", "{}", I, subject, 1)
    store.add_constituent_attribution(content_hash, "folio", root, I, subject)


def test_genesis_of_a_lone_version_is_itself(store):
    g = _folio(store, "solo")
    res = lineage_genesis_for(store, g)
    assert res == Genesis(g, resolved=True)


def test_linear_chain_resolves_to_the_single_genesis(store):
    g = _folio(store, "v1")
    v2 = _folio(store, "v2")
    v3 = _folio(store, "v3")
    _supersede(store, v2, g)   # v2 supersedes v1
    _supersede(store, v3, v2)  # v3 supersedes v2
    # from any version the backward walk lands on the same genesis
    assert lineage_genesis_for(store, v3) == Genesis(g, resolved=True)
    assert lineage_genesis_for(store, v2) == Genesis(g, resolved=True)
    assert lineage_genesis_for(store, g) == Genesis(g, resolved=True)


def test_fork_is_single_valued_per_branch(store):
    """A FORK (two supersedes sharing a to_id = one parent, two children) is ALLOWED:
    each child walks back to the SAME single genesis. Only a MERGE breaks this."""
    g = _folio(store, "v1")
    a = _folio(store, "childA")
    b = _folio(store, "childB")
    _supersede(store, a, g)  # a supersedes v1
    _supersede(store, b, g)  # b ALSO supersedes v1 (fork — shared to_id)
    assert lineage_genesis_for(store, a) == Genesis(g, resolved=True)
    assert lineage_genesis_for(store, b) == Genesis(g, resolved=True)


def test_merge_two_parents_fails_closed(store):
    """A MERGE (one from_id with two supersedes parents) is the single thing that makes
    genesis multi-valued — reject. The partial-unique index normally PREVENTS a stored
    merge (the schema backstop), so this simulates a LEGACY corpus (pre-index) by
    dropping the index, proving lineage_genesis_for's own guard is defense-in-depth."""
    store.conn.execute("DROP INDEX idx_threads_supersedes_one_parent")
    p1 = _folio(store, "p1")
    p2 = _folio(store, "p2")
    child = _folio(store, "child")
    _supersede(store, child, p1)  # child supersedes p1
    _supersede(store, child, p2)  # child ALSO supersedes p2 (merge — shared from_id)
    with pytest.raises(LineageReject):
        lineage_genesis_for(store, child)


def test_self_edge_fails_closed(store):
    v = _folio(store, "v")
    _supersede(store, v, v)  # from == to
    with pytest.raises(LineageReject):
        lineage_genesis_for(store, v)


def test_cycle_fails_closed(store):
    a = _folio(store, "a")
    b = _folio(store, "b")
    _supersede(store, a, b)  # a -> b
    _supersede(store, b, a)  # b -> a  (2-cycle)
    with pytest.raises(LineageReject):
        lineage_genesis_for(store, a)


def test_signed_chain_gap_fails_closed(store):
    """A SIGNED child whose supersedes parent is not held is a forgery signal."""
    child = _folio(store, "child")
    _sign(store, child)  # child is signed
    _supersede(store, child, "sha256::" + "f" * 64)  # parent never admitted
    with pytest.raises(LineageReject):
        lineage_genesis_for(store, child)


def test_legacy_gap_degrades_to_unresolved(store):
    """An UNSIGNED child with an unheld parent is legacy incompleteness — degrade to
    unresolved (owner=None; steward+ path), never a hard reject."""
    child = _folio(store, "child")  # NOT signed
    _supersede(store, child, "sha256::" + "f" * 64)  # unheld parent
    res = lineage_genesis_for(store, child)
    assert res.resolved is False
    assert res.hash == child


def test_pending_same_batch_supersedes_deepen_resolution(store):
    """A same-batch supersedes not yet stored still steers genesis when passed as
    pending (the staged graph)."""
    g = _folio(store, "v1")
    v2 = _folio(store, "v2")
    # v2 -> v1 is only PENDING (not stored). Without it, genesis(v2) is v2.
    assert lineage_genesis_for(store, v2) == Genesis(v2, resolved=True)
    # With it staged, genesis(v2) walks back to v1.
    res = lineage_genesis_for(store, v2, pending_supersedes=[(v2, g)])
    assert res == Genesis(g, resolved=True)


def test_pending_merge_also_fails_closed(store):
    """A pending edge that introduces a second parent is a merge too."""
    p1 = _folio(store, "p1")
    p2 = _folio(store, "p2")
    child = _folio(store, "child")
    _supersede(store, child, p1)  # stored parent
    # pending adds a SECOND parent -> merge
    with pytest.raises(LineageReject):
        lineage_genesis_for(store, child, pending_supersedes=[(child, p2)])
