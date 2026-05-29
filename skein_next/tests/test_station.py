"""Tests for the Slice 3 station service layer and the store read helpers it
leans on (search, short-hash prefix lookup)."""

import pytest

from skein_next.station import AmbiguousReference, Station, UnknownSite, _short, _title_line
from skein_next.store import SkeinNextStore


@pytest.fixture
def station(tmp_path):
    s = Station(data_dir=tmp_path / ".skein-next")
    yield s
    s.close()


# --- store read helpers -----------------------------------------------------


def test_search_matches_title_and_content(tmp_path):
    store = SkeinNextStore(tmp_path / ".skein-next")
    store.create_folio({"type": "finding", "title": "cache collision", "content": "body"})
    store.create_folio({"type": "notion", "title": "unrelated", "content": "shard the cache"})
    store.create_folio({"type": "notion", "title": "nothing", "content": "here"})
    assert len(store.search_folios("cache")) == 2
    assert len(store.search_folios("collision")) == 1
    assert store.search_folios("nonexistent") == []
    store.close()


def test_search_treats_like_wildcards_literally(tmp_path):
    store = SkeinNextStore(tmp_path / ".skein-next")
    store.create_folio({"type": "finding", "title": "100% done", "content": "x"})
    store.create_folio({"type": "finding", "title": "not full", "content": "y"})
    # '%' must match the literal percent sign, not act as a wildcard.
    hits = store.search_folios("100%")
    assert len(hits) == 1 and hits[0]["title"] == "100% done"
    store.close()


def test_find_by_prefix_unique_and_ambiguous(tmp_path):
    store = SkeinNextStore(tmp_path / ".skein-next")
    h = store.create_folio({"type": "finding", "title": "t", "content": "c"})
    prefix = h[: len("sha256::") + 6]
    assert store.find_by_prefix(h) == [h]
    assert store.find_by_prefix(prefix) == [h]
    assert store.find_by_prefix("sha256::ffffffffffff") == []
    store.close()


# --- reference resolution ---------------------------------------------------


def test_resolve_ref_full_hash(station):
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    assert station.resolve_ref(h) == h


def test_resolve_ref_via_alias(station):
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    station.store.set_alias("finding-20260529-abcd", h)
    assert station.resolve_ref("finding-20260529-abcd") == h


def test_resolve_ref_short_prefix(station):
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    assert station.resolve_ref(_short(h)) == h


def test_resolve_ref_ambiguous_prefix_raises(station, monkeypatch):
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    # Force two matches for the same prefix to exercise the ambiguity guard.
    monkeypatch.setattr(station.store, "find_by_prefix", lambda p, limit=10: [h, h + "x"])
    with pytest.raises(AmbiguousReference):
        station.resolve_ref("sha256::dead")


def test_resolve_ref_ambiguous_prefix_real_collision(station):
    # Insert two folio rows that genuinely share a short prefix, so the real
    # find_by_prefix query + len()>1 guard are exercised end-to-end (not mocked).
    shared = "sha256::abcdef000000"
    for tail in ("1111", "2222"):
        station.store.conn.execute(
            "INSERT INTO folios (content_hash, type, title, content) VALUES (?,?,?,?)",
            (shared + tail, "finding", "t", "c"),
        )
    station.store.conn.commit()
    with pytest.raises(AmbiguousReference) as ei:
        station.resolve_ref(shared)
    assert len(ei.value.matches) == 2


def test_resolve_ref_unknown_returns_none(station):
    assert station.resolve_ref("sha256::deadbeef") is None
    assert station.resolve_ref("not-a-real-id") is None


# --- post / membership ------------------------------------------------------


def test_post_requires_existing_site(station):
    with pytest.raises(UnknownSite):
        station.post(type="finding", site="ghost", title="t")


def test_post_creates_folio_and_within_edge(station):
    station.create_site("proj", purpose="a project")
    h = station.post(type="finding", site="proj", title="found it", content="body", created_by="me")
    folio = station.get_folio(h)
    assert folio["type"] == "finding" and folio["title"] == "found it"
    members = station.folios_in_site("proj")
    assert [m["content_hash"] for m in members] == [h]


def test_post_is_idempotent(station):
    station.create_site("proj")
    a = station.post(type="finding", site="proj", title="t", content="c", created_at="2026-01-01T00:00:00Z")
    b = station.post(type="finding", site="proj", title="t", content="c", created_at="2026-01-01T00:00:00Z")
    assert a == b
    # one folio, and exactly one membership edge
    assert len(station.folios_in_site("proj")) == 1


def test_folios_in_site_type_filter(station):
    station.create_site("proj")
    station.post(type="finding", site="proj", title="f", created_at="2026-01-01T00:00:00Z")
    station.post(type="notion", site="proj", title="n", created_at="2026-01-02T00:00:00Z")
    assert len(station.folios_in_site("proj")) == 2
    assert len(station.folios_in_site("proj", type="finding")) == 1


def test_folios_in_site_unknown_raises(station):
    with pytest.raises(UnknownSite):
        station.folios_in_site("ghost")


def test_folios_in_site_limit_returns_earliest_by_created_at(station):
    # The MAJOR fell-r1 finding: limit must apply *after* created_at ordering,
    # not truncate the (timestamp-less, hash-ordered) membership edges. Post 5
    # folios with ascending timestamps; limit=3 must return the earliest three.
    station.create_site("proj")
    hashes = [
        station.post(
            type="finding", site="proj", title=f"f{i}",
            created_at=f"2026-01-0{i + 1}T00:00:00Z",
        )
        for i in range(5)
    ]
    got = station.folios_in_site("proj", limit=3)
    assert [f["content_hash"] for f in got] == hashes[:3]


def test_create_site_idempotent_preserves_membership(station):
    # The whole reason create_site is idempotent: a member posted before a
    # re-create must still resolve through the (unchanged) site folio.
    station.create_site("proj", purpose="first")
    h = station.post(type="finding", site="proj", title="member", created_at="2026-01-01T00:00:00Z")
    station.create_site("proj", purpose="a different purpose")  # must NOT remint
    members = station.folios_in_site("proj")
    assert [m["content_hash"] for m in members] == [h]


# --- sites ------------------------------------------------------------------


def test_list_sites_and_get_site(station):
    station.create_site("alpha", purpose="first")
    station.create_site("beta", purpose="second")
    pairs = station.list_sites()
    assert [slug for slug, _ in pairs] == ["alpha", "beta"]
    assert station.get_site("alpha")["content"] == "first"
    assert station.get_site("missing") is None


# --- thread graph -----------------------------------------------------------


def test_thread_graph_separates_links_and_membership(station):
    station.create_site("proj")
    a = station.post(type="finding", site="proj", title="A", created_at="2026-01-01T00:00:00Z")
    b = station.post(type="brief", site="proj", title="B", created_at="2026-01-02T00:00:00Z")
    station.store.save_thread(from_id=a, to_id=b, type="supersedes")

    graph = station.thread_graph(a)
    assert graph["content_hash"] == a
    assert len(graph["outgoing"]) == 1
    assert graph["outgoing"][0]["type"] == "supersedes"
    assert graph["outgoing"][0]["peer"]["kind"] == "folio"
    assert graph["outgoing"][0]["peer"]["folio"]["content_hash"] == b
    # the within edge to the site is reported as membership, not a link
    assert len(graph["memberships"]) == 1
    assert graph["memberships"][0]["peer"]["folio"]["type"] == "site"


def test_thread_graph_unresolved_peer(station):
    station.create_site("proj")
    a = station.post(type="finding", site="proj", title="A")
    station.store.save_thread(from_id=a, to_id="other:finding-20260101-zzzz", type="relates")
    graph = station.thread_graph(a)
    link = [e for e in graph["outgoing"] if e["type"] == "relates"][0]
    assert link["peer"]["kind"] == "ref"
    assert link["peer"]["id"] == "other:finding-20260101-zzzz"


def test_thread_graph_unknown_ref_returns_none(station):
    assert station.thread_graph("sha256::deadbeef") is None


# --- helpers ----------------------------------------------------------------


def test_short_and_title_line():
    assert _short("sha256::" + "a" * 64, 8) == "sha256::aaaaaaaa"
    assert _title_line("  first line\nsecond") == "first line"
    assert _title_line("x" * 200, limit=10) == "x" * 9 + "…"
    assert _title_line(None) == ""
