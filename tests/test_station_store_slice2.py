"""Slice 2 store extensions: batch transactions, slugs, aliases, counts, and the
unresolved-endpoint query — a Stage-7a migration guard.

Re-homed from skein_next/tests/test_store_slice2.py (station re-home Stage 5), over the
re-homed ``StationStore`` (Fork B). Adaptations from the skein_next source, all faithful to
the strict-null station narrowing (finding-20260709-18zn):

- ``SkeinNextStore`` → ``StationStore``; a second connection opens by ``db_path`` (the
  station store carries no ``data_dir`` attr).
- Every ``save_thread`` carries a ``created_at`` — the station requires non-null
  from_id/to_id/type/created_at (the real producer, a workbench publish, always does).
- The null-endpoint "message" row the skein_next case used (an actor dropped to
  ``weaver`` on import, from_id=NULL) is OMITTED: the station's ``threads`` table declares
  ``from_id``/``to_id`` NOT NULL, so a null endpoint is structurally impossible there. The
  ``unresolved_endpoints`` IS-NOT-NULL clause is a byte-faithful carry whose NULL branch
  cannot arise on this schema; the legacy-id assertion is unchanged.

``set_alias`` / ``count_threads`` / ``unresolved_endpoints`` are byte-faithful additive
accessors ported alongside this suite (they touch only ``aliases``/``threads``, both
re-homed) so the migration guard keeps its assertions.
"""

import pytest

from skein.station_store import StationStore


@pytest.fixture
def store(tmp_path):
    s = StationStore(data_dir=tmp_path / ".skein-next")
    yield s
    s.close()


FOLIO = {
    "type": "finding",
    "title": "a finding",
    "content": "what was discovered",
    "created_at": "2026-05-29T14:47:52Z",
    "created_by": "mote-0529",
}

TS = "2026-05-29T14:47:52Z"  # a concrete created_at for the strict-null thread edges


# --- batch / transaction ----------------------------------------------------


def test_transaction_persists_all_writes(store):
    with store.transaction():
        for i in range(50):
            store.create_folio({**FOLIO, "title": f"f{i}"})
    assert store.count_folios() == 50


def test_transaction_rolls_back_on_error(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.create_folio(FOLIO)
            raise RuntimeError("boom")
    # nothing committed
    assert store.count_folios() == 0


def test_transaction_idempotent_inside_batch(store):
    with store.transaction():
        store.create_folio(FOLIO)
        store.create_folio(FOLIO)
    assert store.count_folios() == 1


def test_writes_outside_transaction_still_commit(store):
    store.create_folio(FOLIO)
    # a fresh connection to the same file sees it -> it was committed
    other = StationStore(db_path=store.db_path)
    try:
        assert other.count_folios() == 1
    finally:
        other.close()


def test_nested_transaction_is_rejected(store):
    with store.transaction():
        with pytest.raises(RuntimeError):
            with store.transaction():
                pass


# --- slugs ------------------------------------------------------------------


def test_slug_set_and_resolve(store):
    h = store.create_folio({**FOLIO, "type": "site"})
    store.set_slug("skein-mesh", h)
    assert store.resolve_slug("skein-mesh") == h


def test_resolve_unknown_slug_returns_none(store):
    assert store.resolve_slug("nope") is None


def test_slug_upsert(store):
    h1 = store.create_folio({**FOLIO, "title": "site one"})
    h2 = store.create_folio({**FOLIO, "title": "site two"})
    store.set_slug("s", h1)
    store.set_slug("s", h2)
    assert store.resolve_slug("s") == h2


def test_list_slugs(store):
    h = store.create_folio({**FOLIO, "type": "site"})
    store.set_slug("a", h)
    store.set_slug("b", h)
    got = dict(store.list_slugs())
    assert got == {"a": h, "b": h}


# --- unresolved endpoints ---------------------------------------------------


def test_unresolved_endpoints_lists_only_unresolved_legacy_ids(store):
    folio_hash = store.create_folio(FOLIO)
    other_hash = store.create_folio({**FOLIO, "title": "other"})
    # a resolved folio edge: both endpoints are real hashes -> not unresolved
    store.save_thread(from_id=folio_hash, to_id=other_hash, type="reference", created_at=TS)
    # a dangling legacy-id endpoint with no alias -> unresolved
    store.save_thread(from_id=folio_hash, to_id="brief-20260101-dang", type="mention", created_at=TS)
    # a cross-project colon ref -> unresolved
    store.save_thread(from_id="otherproj:brief-20260101-xprj", to_id=folio_hash,
                      type="reference", created_at=TS)

    got = set(store.unresolved_endpoints())
    assert got == {"brief-20260101-dang", "otherproj:brief-20260101-xprj"}


def test_unresolved_endpoints_survives_null_alias_key(store):
    # legacy_id is a nullable TEXT PK, so a NULL alias key can exist. Without guarding
    # the subquery, one NULL makes `endpoint NOT IN (SELECT legacy_id ...)` evaluate to
    # NULL/false for EVERY row (SQL three-valued logic) and the query silently returns
    # [] — the migration guard would go quiet. Hardened past skein_next's raw query.
    folio_hash = store.create_folio(FOLIO)
    store.save_thread(from_id=folio_hash, to_id="brief-20260101-dang", type="mention", created_at=TS)
    store.conn.execute("INSERT INTO aliases (legacy_id, content_hash) VALUES (NULL, ?)", (folio_hash,))
    store.conn.commit()
    assert store.unresolved_endpoints() == ["brief-20260101-dang"]


def test_unresolved_endpoint_resolves_once_alias_exists(store):
    folio_hash = store.create_folio(FOLIO)
    store.save_thread(from_id=folio_hash, to_id="brief-20260101-late", type="mention", created_at=TS)
    assert "brief-20260101-late" in store.unresolved_endpoints()
    # the target later imports and registers its alias
    target = store.create_folio({**FOLIO, "title": "late arrival"})
    store.set_alias("brief-20260101-late", target)
    assert "brief-20260101-late" not in store.unresolved_endpoints()


# --- counts -----------------------------------------------------------------


def test_counts(store):
    assert store.count_folios() == 0
    assert store.count_threads() == 0
    h = store.create_folio(FOLIO)
    store.save_thread(from_id=h, to_id=h, type="status", content="closed", created_at=TS)
    assert store.count_folios() == 1
    assert store.count_threads() == 1
