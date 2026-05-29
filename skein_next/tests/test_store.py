"""Tests for the content-hash-native store: folios, threads, aliases."""

from datetime import datetime, timezone

import pytest

from skein_next.store import SkeinNextStore


@pytest.fixture
def store(tmp_path):
    s = SkeinNextStore(data_dir=tmp_path / ".skein-next")
    yield s
    s.close()


FOLIO = {
    "type": "finding",
    "title": "a finding",
    "content": "what was discovered",
    "created_at": "2026-05-29T14:47:52Z",
    "created_by": "mote-0529",
}


# --- folio create / get round-trip ------------------------------------------


def test_create_returns_address_hash(store):
    h = store.create_folio(FOLIO)
    assert h.startswith("sha256::")


def test_round_trip(store):
    h = store.create_folio(FOLIO)
    got = store.get_folio(h)
    assert got is not None
    assert got["content_hash"] == h
    assert got["type"] == FOLIO["type"]
    assert got["title"] == FOLIO["title"]
    assert got["content"] == FOLIO["content"]
    assert got["created_by"] == FOLIO["created_by"]
    # created_at is stored in canonical normalized form
    assert got["created_at"] == "2026-05-29T14:47:52+00:00"


def test_get_missing_returns_none(store):
    assert store.get_folio("sha256::" + "0" * 64) is None


def test_created_at_stored_normalized_regardless_of_input(store):
    h1 = store.create_folio({**FOLIO, "created_at": datetime(2026, 5, 29, 14, 47, 52)})
    h2 = store.create_folio({**FOLIO, "created_at": "2026-05-29 14:47:52"})
    assert h1 == h2
    assert store.get_folio(h1)["created_at"] == "2026-05-29T14:47:52+00:00"


# --- idempotency ------------------------------------------------------------


def test_idempotent_insert(store):
    h1 = store.create_folio(FOLIO)
    h2 = store.create_folio(FOLIO)
    assert h1 == h2
    rows = store.list_folios()
    assert len(rows) == 1


def test_idempotent_across_created_at_encodings(store):
    """Same instant in different encodings dedups to one row."""
    store.create_folio({**FOLIO, "created_at": "2026-05-29T14:47:52Z"})
    store.create_folio({**FOLIO, "created_at": "2026-05-29T14:47:52+00:00"})
    store.create_folio({**FOLIO, "created_at": datetime(2026, 5, 29, 14, 47, 52)})
    assert len(store.list_folios()) == 1


# --- list / paging ----------------------------------------------------------


def test_list_folios_paging(store):
    for i in range(5):
        store.create_folio({**FOLIO, "title": f"folio {i}"})
    assert len(store.list_folios()) == 5
    assert len(store.list_folios(limit=2)) == 2
    page1 = store.list_folios(limit=2, offset=0)
    page2 = store.list_folios(limit=2, offset=2)
    assert {r["content_hash"] for r in page1}.isdisjoint(
        {r["content_hash"] for r in page2}
    )


# --- threads ----------------------------------------------------------------


def test_save_and_query_thread(store):
    th = store.save_thread(
        from_id="sha256::" + "a" * 64,
        to_id="sha256::" + "b" * 64,
        type="reply",
        weaver="mote-0529",
        created_at="2026-05-29T14:47:52Z",
        content="a reply",
    )
    assert th.startswith("sha256::")
    rows = store.get_threads(from_id="sha256::" + "a" * 64)
    assert len(rows) == 1
    assert rows[0]["thread_hash"] == th
    assert rows[0]["type"] == "reply"
    assert rows[0]["weaver"] == "mote-0529"
    assert rows[0]["created_at"] == "2026-05-29T14:47:52+00:00"


def test_thread_endpoints_are_not_classified(store):
    """from_id/to_id store folio-hashes or actor ids alike, without classification."""
    store.save_thread(from_id="agent-bob", to_id="session-xyz", type="message")
    rows = store.get_threads(from_id="agent-bob")
    assert len(rows) == 1
    assert rows[0]["to_id"] == "session-xyz"


def test_thread_query_filters(store):
    a, b, c = ("sha256::" + ch * 64 for ch in "abc")
    store.save_thread(from_id=a, to_id=b, type="reply")
    store.save_thread(from_id=a, to_id=c, type="mention")
    store.save_thread(from_id=b, to_id=c, type="reply")

    assert len(store.get_threads(from_id=a)) == 2
    assert len(store.get_threads(to_id=c)) == 2
    assert len(store.get_threads(type="reply")) == 2
    assert len(store.get_threads(from_id=a, type="mention")) == 1
    assert len(store.get_threads(to_id=c, type="reply")) == 1


def test_thread_idempotent(store):
    kw = dict(from_id="x", to_id="y", type="reply", created_at="2026-05-29T14:47:52Z")
    h1 = store.save_thread(**kw)
    h2 = store.save_thread(**kw)
    assert h1 == h2
    assert len(store.get_threads(from_id="x")) == 1


# --- aliases ----------------------------------------------------------------


def test_alias_set_and_resolve(store):
    h = store.create_folio(FOLIO)
    store.set_alias("brief-20260529-i6fy", h)
    assert store.resolve_alias("brief-20260529-i6fy") == h


def test_resolve_unknown_alias_returns_none(store):
    assert store.resolve_alias("brief-does-not-exist") is None


def test_alias_reassignment(store):
    h1 = store.create_folio(FOLIO)
    h2 = store.create_folio({**FOLIO, "title": "another"})
    store.set_alias("legacy-1", h1)
    store.set_alias("legacy-1", h2)
    assert store.resolve_alias("legacy-1") == h2


# --- persistence ------------------------------------------------------------


def test_persists_across_reopen(tmp_path):
    data_dir = tmp_path / ".skein-next"
    s1 = SkeinNextStore(data_dir=data_dir)
    h = s1.create_folio(FOLIO)
    s1.set_alias("legacy-1", h)
    s1.close()

    s2 = SkeinNextStore(data_dir=data_dir)
    assert s2.get_folio(h) is not None
    assert s2.resolve_alias("legacy-1") == h
    s2.close()
