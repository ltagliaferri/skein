"""Tests for the Slice 4 web read surface: the content-hash adapter and the
FastAPI routes driven through TestClient.

The adapter is the interesting seam (hash->folio_id, slug->site_id, ISO->datetime,
thread-derived status, rebuilt cross-refs); the route tests confirm the templates
render against it and the per-request connection works under TestClient's threads.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from skein_next.station import Station
from skein_next.web.adapter import ContentHashAdapter, _to_datetime
from skein_next.web.app import ENV_DATA_DIR, create_app


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / ".skein-next"


@pytest.fixture
def seeded(data_dir):
    """A station with two linked folios, a status thread, and a sub-site."""
    with Station(data_dir) as st:
        st.create_site("proj", purpose="the project")
        a = st.post(type="finding", site="proj", title="Finding A", content="body A",
                    created_by="alice", created_at="2026-01-01T00:00:00Z")
        b = st.post(type="brief", site="proj", title="Brief B", content="body B",
                    created_by="bob", created_at="2026-01-02T00:00:00Z")
        # a references b (a real link, should surface as a cross-ref)
        st.store.save_thread(from_id=a, to_id=b, type="reference",
                             created_at="2026-01-03T00:00:00Z")
        # a status thread closing b (status is thread-derived, not a column)
        st.store.save_thread(to_id=b, type="status", content="closed",
                             created_at="2026-01-04T00:00:00Z")
    return {"data_dir": data_dir, "a": a, "b": b}


@pytest.fixture
def client(seeded, monkeypatch):
    monkeypatch.setenv(ENV_DATA_DIR, str(seeded["data_dir"]))
    return TestClient(create_app())


# --- adapter ----------------------------------------------------------------


def test_to_datetime_handles_iso_and_missing():
    assert _to_datetime("2026-01-01T00:00:00+00:00").year == 2026
    # naive string is treated as UTC
    assert _to_datetime("2026-01-01T00:00:00").tzinfo is not None
    # None / garbage become the aware sentinel (no crash on sort/strftime)
    assert _to_datetime(None) == datetime.min.replace(tzinfo=timezone.utc)
    assert _to_datetime("not-a-date") == datetime.min.replace(tzinfo=timezone.utc)


def test_adapter_maps_hash_to_folio_id_and_slug_to_site(seeded):
    ad = ContentHashAdapter(seeded["data_dir"])
    try:
        folio = ad.get_folio(seeded["a"])
        assert folio.folio_id == seeded["a"] == folio.content_hash
        assert folio.site_id == "proj"
        assert isinstance(folio.created_at, datetime)
        sites = ad.get_sites()
        assert [s.site_id for s in sites] == ["proj"]
        assert sites[0].purpose == "the project"
    finally:
        ad.close()


def test_adapter_status_is_thread_derived(seeded):
    ad = ContentHashAdapter(seeded["data_dir"])
    try:
        # b was closed via a status thread; a has none -> default open
        assert ad.get_folio(seeded["b"]).status == "closed"
        assert ad.get_folio(seeded["a"]).status == "open"
    finally:
        ad.close()


def test_adapter_get_folio_via_alias(seeded):
    with Station(seeded["data_dir"]) as st:  # writable setup; the adapter reads RO
        st.store.set_alias("finding-20260101-leg1", seeded["a"])
    ad = ContentHashAdapter(seeded["data_dir"])
    try:
        assert ad.get_folio("finding-20260101-leg1").content_hash == seeded["a"]
        assert ad.get_folio("totally-unknown") is None
    finally:
        ad.close()


def test_adapter_cross_refs_from_thread_graph(seeded):
    ad = ContentHashAdapter(seeded["data_dir"])
    try:
        # a -> b reference must surface; the within (membership) and status
        # edges must NOT be treated as cross-refs.
        refs_a = ad.cross_refs(seeded["a"])
        assert [r.content_hash for r in refs_a] == [seeded["b"]]
        # b sees a from the incoming reference; neither sees its own site folio.
        refs_b = ad.cross_refs(seeded["b"])
        assert seeded["a"] in [r.content_hash for r in refs_b]
        assert all(r.type != "site" for r in refs_a + refs_b)
    finally:
        ad.close()


def test_adapter_cross_refs_resolves_alias_endpoint(seeded):
    # A thread endpoint kept as a legacy id resolves to a folio via alias.
    with Station(seeded["data_dir"]) as st:  # writable setup; the adapter reads RO
        st.store.set_alias("brief-20260101-old0", seeded["b"])
        st.store.save_thread(from_id=seeded["a"], to_id="brief-20260101-old0",
                             type="relates", created_at="2026-01-05T00:00:00Z")
    ad = ContentHashAdapter(seeded["data_dir"])
    try:
        targets = [r.content_hash for r in ad.cross_refs(seeded["a"])]
        assert seeded["b"] in targets  # still just b, deduped
        assert targets.count(seeded["b"]) == 1
    finally:
        ad.close()


def test_adapter_cross_refs_excludes_alias_self_loop(seeded):
    # fell-r1 MINOR: an edge to a legacy id that aliases back to THIS folio must
    # not list the folio as its own reference (guard the resolved target).
    with Station(seeded["data_dir"]) as st:  # writable setup; the adapter reads RO
        st.store.set_alias("finding-20260101-self", seeded["a"])
        st.store.save_thread(from_id=seeded["a"], to_id="finding-20260101-self",
                             type="relates", created_at="2026-01-06T00:00:00Z")
    ad = ContentHashAdapter(seeded["data_dir"])
    try:
        assert seeded["a"] not in [r.content_hash for r in ad.cross_refs(seeded["a"])]
    finally:
        ad.close()


def test_latest_statuses_tiebreak_is_deterministic(seeded):
    # fell-r1 MINOR: two status threads sharing a created_at must resolve to a
    # stable winner (ORDER BY created_at, thread_hash), not flip per query.
    b = seeded["b"]
    ts = "2026-02-02T00:00:00+00:00"
    with Station(seeded["data_dir"]) as st:  # writable setup; the adapter reads RO
        h_open = st.store.save_thread(to_id=b, type="status", content="reopened", created_at=ts)
        h_closed = st.store.save_thread(to_id=b, type="status", content="archived", created_at=ts)
    # ascending (created_at, thread_hash) -> the greater thread_hash wins
    winner = "reopened" if h_open > h_closed else "archived"
    ad = ContentHashAdapter(seeded["data_dir"])
    try:
        first = ad.store.latest_statuses([b])[b]
        second = ad.store.latest_statuses([b])[b]
        assert first == second == winner
    finally:
        ad.close()


# --- routes -----------------------------------------------------------------


def test_index_lists_sites_and_recent(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "proj" in r.text and "the project" in r.text
    assert "Finding A" in r.text and "Brief B" in r.text


def test_site_detail_and_type_filter(client):
    r = client.get("/site/proj")
    assert r.status_code == 200 and "Finding A" in r.text and "Brief B" in r.text
    r = client.get("/site/proj", params={"type": "finding"})
    assert "Finding A" in r.text and "Brief B" not in r.text


def test_site_404(client):
    assert client.get("/site/ghost").status_code == 404


def test_folio_detail_shows_provenance_and_crossref(client, seeded):
    r = client.get(f"/folio/{seeded['a']}")
    assert r.status_code == 200
    assert seeded["a"] in r.text          # provenance content hash
    assert "Provenance" in r.text
    assert "body A" in r.text             # rendered markdown
    assert "References" in r.text         # cross-ref section present
    assert "Brief B" in r.text            # the referenced folio


def test_folio_closed_status_renders(client, seeded):
    r = client.get(f"/folio/{seeded['b']}")
    assert r.status_code == 200 and "closed" in r.text


def test_folio_404(client):
    assert client.get("/folio/sha256::deadbeef").status_code == 404


def test_search_route(client):
    r = client.get("/search", params={"q": "body"})
    assert r.status_code == 200
    assert "Finding A" in r.text and "Brief B" in r.text
    r = client.get("/search", params={"q": "no-such-text-anywhere"})
    assert r.status_code == 200 and "No matches" in r.text


def test_concurrent_requests_isolated_connections(client):
    # Per-request adapter + check_same_thread=False must serve concurrent
    # requests across threads with no connection errors (regression guard for
    # the store's single-shared-connection hazard).
    import concurrent.futures

    def hit(_):
        return client.get("/").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        codes = list(ex.map(hit, range(40)))
    assert codes == [200] * 40
