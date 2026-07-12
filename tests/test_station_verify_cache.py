"""Thread D — verify_cache mechanics: the STORE contract + ingress write (VC1-VC6,
VC10, VC11, harden-C) PLUS the read-verdict cache-behavior cells (VC7/VC8/VC14 + the
tampered-body integrity gate).

Re-homed from skein_next/tests/test_verify_cache.py: the store/ingress cells landed at
station re-home Stage 3 over the ``StationStore`` verify_cache accessors + the re-homed
ingress write path; the read-verdict cells landed at Stage 4 with the read surface (they
exercise ``envelope.folio_verdict`` render behavior). The cache stores ONLY the manifest
SIGNATURE verdict (step 3), keyed on (manifest_hash, bundle_hash). The INGRESS is the sole
writer; a read app opens mode=ro and never writes. Recoverable statuses are never cached;
a table-absent read degrades to a cache MISS, never a 500. The SIGNED-verdict cache-hit
round-trip also has end-to-end coverage in
tests/test_station_e2e_publish.py::test_e2e_read_cache_hit_after_ingest.
"""

from __future__ import annotations

import json

import pytest

from skein import envelope as env_mod
from skein import sign as sign_mod
from skein import signing
from skein import wire
from skein.canon import manifest_descriptor_canonical_bytes
from skein.identity import content_hash_for_bytes
from skein.ingress import ingest
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus
from skein.station import Station
from skein.station_store import StationStore, bundle_hash_for

from tests import station_publish_helpers as h
from tests.station_publish_helpers import I, ALICE
from tests.station_read_helpers import StationBuilder

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


def test_verify_cache_keyed_on_both_hashes(store):  # VC2
    store.verify_cache_put(MH, "bh1", "VERIFIED")
    assert store.verify_cache_get(MH, "bh2") is None


def test_verify_cache_skips_trust_root_stale(store):  # VC3
    store.verify_cache_put(MH, BH, "TRUST_ROOT_STALE")
    assert store.verify_cache_get(MH, BH) is None


def test_verify_cache_skips_offline_no_root(store):  # VC4
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


def test_ingest_populates_manifest_verdict(tmp_path):  # VC6
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


def test_verdict_missing_cache_table_is_miss(tmp_path):  # VC10
    s = StationStore(tmp_path / ".skein-next")
    s.conn.execute("DROP TABLE verify_cache")
    s.conn.commit()
    assert s.verify_cache_get(MH, BH) is None  # a MISS, not an OperationalError
    s.close()


def test_cache_get_propagates_real_faults(store):  # harden C
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


# --- VC7/VC8/VC14 + integrity gate: read-side verdict cache behavior ---------
#
# DEFERRED from Stage 3 (this file's docstring): these exercise
# ``envelope.folio_verdict`` render behavior, so they ride with the read surface in
# Stage 4. The read app NEVER writes the cache (VC7); a warm cache elides the
# Sigstore step (VC8); and the cache is an OPTIMIZATION, never a correctness
# dependency — a cold, warm, or absent cache all reach the same verdict (VC14). The
# integrity gate: a tampered body re-hashes off its content_hash key FIRST, so it
# reads "NOT VERIFIED — integrity" even on a warm cache that would otherwise elide
# the signature step (harden-A / VC12). Client content is built via the StationBuilder
# read-corpus helper — the skein_next Station.create_site/post authoring verbs are DROP.


def _cover(store, content_hash, *, cache_status=None, bind=True, subject=ALICE):
    """Cover a folio with a signed manifest (+ optional binding + warm cache row)."""
    ms = sign_mod.sign_manifest([content_hash], h.make_signer(subject=subject))
    d = ms["descriptor"]
    mh = content_hash_for_bytes(manifest_descriptor_canonical_bytes(d["root"], d["leaf_count"]))
    with store.transaction():
        store.add_manifest(d["root"], mh, json.dumps(d, sort_keys=True),
                           json.dumps(ms["leaf_list"]), ms["signature_bundle"],
                           I, subject, d["leaf_count"])
        store.add_constituent_attribution(content_hash, "folio", d["root"], I, subject)
        if cache_status:
            store.verify_cache_put(mh, bundle_hash_for(ms["signature_bundle"]), cache_status, I, subject)
    if bind:
        store.add_binding(I, subject, role="author")
    return mh


def _seed_folio(base):
    """Seed a one-folio station corpus under ``base/.skein-next``; return
    ``(builder, folio_hash)``. The caller covers via ``_cover`` and ``close()``s it."""
    st = StationBuilder(base / ".skein-next")
    st.create_site("s", purpose="p", created_by="t")
    fh = st.post("finding", "s", "T", "b", created_by="t")
    return st, fh


def test_verdict_cache_miss_does_not_write(tmp_path, monkeypatch):  # VC7
    st, fh = _seed_folio(tmp_path)
    _cover(st.store, fh, cache_status=None, bind=True)  # cold cache
    st.close()
    ro = StationStore(tmp_path / ".skein-next", read_only=True)
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
            overall=VerifyStatus.VERIFIED),
    )
    verdict, _ = env_mod.folio_verdict(ro, fh, ro.get_folio(fh))
    assert verdict.startswith("SIGNED")  # correct verdict via in-process verify
    ro.close()
    # nothing was written to the cache (the read app never writes)
    check = StationStore(tmp_path / ".skein-next")
    assert check.conn.execute("SELECT COUNT(*) FROM verify_cache").fetchone()[0] == 0
    check.close()


def test_verdict_cache_hit_skips_sigstore(tmp_path):  # VC8
    st, fh = _seed_folio(tmp_path)
    _cover(st.store, fh, cache_status="VERIFIED", bind=True)  # warm
    calls = []
    orig = sign_mod.signing.verify_multi
    try:
        sign_mod.signing.verify_multi = lambda cb, b: calls.append(1) or orig(cb, b)
        verdict, _ = env_mod.folio_verdict(st.store, fh, st.store.get_folio(fh))
    finally:
        sign_mod.signing.verify_multi = orig
    assert verdict.startswith("SIGNED")
    assert calls == []  # warm cache elided the Sigstore step
    st.close()


def test_correct_with_cold_or_absent_cache(tmp_path, monkeypatch):  # VC14
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
            overall=VerifyStatus.VERIFIED),
    )
    # warm
    stw, hw = _seed_folio(tmp_path / "warm")
    _cover(stw.store, hw, cache_status="VERIFIED", bind=True)
    vw, _ = env_mod.folio_verdict(stw.store, hw, stw.store.get_folio(hw))
    stw.close()
    # cold (no cache row)
    stc, hc = _seed_folio(tmp_path / "cold")
    _cover(stc.store, hc, cache_status=None, bind=True)
    vc, _ = env_mod.folio_verdict(stc.store, hc, stc.store.get_folio(hc))
    stc.close()
    # absent table
    sta, ha = _seed_folio(tmp_path / "absent")
    _cover(sta.store, ha, cache_status=None, bind=True)
    sta.store.conn.execute("DROP TABLE verify_cache")
    sta.store.conn.commit()
    va, _ = env_mod.folio_verdict(sta.store, ha, sta.store.get_folio(ha))
    sta.close()
    assert vw == vc == va  # the cache is an optimization, not a correctness dependency
    assert vw.startswith("SIGNED")


@pytest.mark.parametrize("warm", [True, False], ids=["warm-cache", "cold-cache"])
def test_tampered_body_reads_integrity_fail(tmp_path, monkeypatch, warm):
    # An honest signed verdict path so the ONLY thing that changes is the tamper.
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
            overall=VerifyStatus.VERIFIED),
    )
    st, fh = _seed_folio(tmp_path)
    _cover(st.store, fh, cache_status="VERIFIED" if warm else None, bind=True)

    # Baseline: an untampered row reads SIGNED through the same path.
    verdict, ident = env_mod.folio_verdict(st.store, fh, st.store.get_folio(fh))
    assert verdict.startswith("SIGNED") and ident is not None

    # Tamper the stored body directly (the address key is unchanged), so the body no
    # longer hashes to its content_hash. Station folios live in ``versions`` (the flat
    # ``folios`` table is retired), so the tamper targets ``versions``.
    st.store.conn.execute(
        "UPDATE versions SET content = ? WHERE content_hash = ?",
        ("tampered body — was 'b'", fh),
    )
    st.store.conn.commit()

    verdict, ident = env_mod.folio_verdict(st.store, fh, st.store.get_folio(fh))
    assert verdict == "NOT VERIFIED — integrity"
    assert ident is None
    st.close()
