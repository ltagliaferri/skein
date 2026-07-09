"""Thread D — verify_cache mechanics: the STORE contract + ingress write (VC1-VC6,
VC10, VC11, harden-C).

Re-homed from skein_next/tests/test_verify_cache.py (station re-home Stage 3), over the
``StationStore`` verify_cache accessors + the re-homed ingress write path. The cache stores
ONLY the manifest SIGNATURE verdict (step 3), keyed on (manifest_hash, bundle_hash). The
INGRESS is the sole writer; a read app opens mode=ro and never writes. Recoverable statuses
are never cached; a table-absent read degrades to a cache MISS, never a 500.

The read-VERDICT cache-behavior cells (VC7/VC8/VC14 + the tampered-body integrity gate)
exercise ``envelope.folio_verdict`` render behavior and ride with the read surface in
Stage 4 (with test_web / test_render); the SIGNED-verdict cache-hit round-trip already has
end-to-end coverage in tests/test_station_e2e_publish.py::test_e2e_read_cache_hit_after_ingest.
"""

from __future__ import annotations

import json

import pytest

from skein import wire
from skein.station_store import StationStore, bundle_hash_for
from skein.station import Station
from skein.ingress import ingest

from tests import station_publish_helpers as h
from tests.station_publish_helpers import I, ALICE

MH = "sha256::" + "a" * 64
BH = "bundlehash-1"


@pytest.fixture
def store(tmp_path):
    s = StationStore(tmp_path / ".skein-next")
    yield s
    s.close()


# --- VC1-VC5: cache put/get + recoverable-status non-caching -----------------


def test_verify_cache_miss_then_hit(store):  # VC1
    assert store.verify_cache_get(MH, BH) is None
    store.verify_cache_put(MH, BH, "VERIFIED", I, ALICE)
    row = store.verify_cache_get(MH, BH)
    assert row["status"] == "VERIFIED" and row["subject"] == ALICE


def test_verify_cache_keyed_on_manifest_and_bundle_hash(store):  # VC2
    store.verify_cache_put(MH, "bh1", "VERIFIED")
    assert store.verify_cache_get(MH, "bh2") is None


def test_verify_cache_never_caches_trust_root_stale(store):  # VC3
    store.verify_cache_put(MH, BH, "TRUST_ROOT_STALE")
    assert store.verify_cache_get(MH, BH) is None


def test_verify_cache_never_caches_offline_no_trusted_root(store):  # VC4
    store.verify_cache_put(MH, BH, "OFFLINE_NO_TRUSTED_ROOT")
    assert store.verify_cache_get(MH, BH) is None


@pytest.mark.parametrize("status", [
    "VERIFIED", "SIGNATURE_MISMATCH", "CERT_INVALID",
    "INCLUSION_FAILED", "IDENTITY_MISMATCH", "BUNDLE_MALFORMED",
])
def test_verify_cache_caches_stable_statuses(store, status):  # VC5
    store.verify_cache_put(MH, BH, status)
    assert store.verify_cache_get(MH, BH)["status"] == status


# --- VC6/VC11: the ingress is the writer; one shared bundle_hash helper -------


def test_ingress_populates_manifest_verdict_at_ingest(tmp_path):  # VC6
    instance = Station(tmp_path / "instance" / ".skein-next")
    instance.store.add_binding(I, ALICE, role="author")
    folios, threads, slugs = h.specs_set()
    batch = wire.build_batch(folios, threads, slugs)
    batch["manifest_signature"] = h.manifest_over(folios, threads)
    ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    descriptor = batch["manifest_signature"]["descriptor"]
    from skein.canon import manifest_descriptor_canonical_bytes
    from skein.identity import content_hash_for_bytes
    mh = content_hash_for_bytes(
        manifest_descriptor_canonical_bytes(descriptor["root"], descriptor["leaf_count"])
    )
    bh = bundle_hash_for(batch["manifest_signature"]["signature_bundle"])
    assert instance.store.verify_cache_get(mh, bh)["status"] == "VERIFIED"
    instance.close()


def test_bundle_hash_one_shared_helper():  # VC11
    bundle_json = json.dumps({"a": 1, "b": [2, 3]})
    assert bundle_hash_for(bundle_json) == bundle_hash_for(bundle_json)
    assert bundle_hash_for(bundle_json) != bundle_hash_for(bundle_json + " ")


# --- VC10: table-absent degrades to a cache miss -----------------------------


def test_read_verdict_on_missing_verify_cache_table_degrades(tmp_path):  # VC10
    s = StationStore(tmp_path / ".skein-next")
    s.conn.execute("DROP TABLE verify_cache")
    s.conn.commit()
    assert s.verify_cache_get(MH, BH) is None  # a MISS, not an OperationalError
    s.close()


def test_verify_cache_get_propagates_non_table_operational_error(store):  # harden C
    """Only the missing-table OperationalError is a cache miss; a real fault
    ("database is locked", I/O error, SQL bug) must PROPAGATE, not be masked as a
    miss that silently degrades every read to in-process verify (VC10 scope)."""
    import sqlite3

    class _LockedConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    real = store.conn
    store.conn = _LockedConn()
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            store.verify_cache_get(MH, BH)
    finally:
        store.conn = real  # restore so the fixture teardown can close cleanly
