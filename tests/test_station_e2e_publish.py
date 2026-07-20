"""Thread F2 — offline end-to-end dress rehearsal (E1-E6).

Re-homed from skein_next/tests/test_e2e_publish.py (station re-home Stage 3). E1 proves the
REAL signing primitive plugs into publish->ingest->read offline (via signing._test_factory,
no network) over the re-homed ``skein.ingress`` + ``StationStore`` + the wired
``skein.envelope.folio_verdict`` read verdict. E2-E6 prove the rejection/acceptance LOGIC of
the unified manifest gate end-to-end.

Client content is built directly (``tests/station_publish_helpers``) and published through
``skein.publish.publish`` (the API-side signed-batch assembler) — the skein_next
station-reading publish + its dry-run path are DROP authoring surfaces, not re-homed here.
E10/E11 (read-open mode=ro / immutable / WAL-visibility) are covered by the Stage-1 store
suite (tests/test_station_store.py::test_read_only_open_*); a station-posture "read sees
committed write" round-trip is kept below.
"""

from __future__ import annotations

import base64
import json

import pytest

from skein import signing
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus

from skein import sign as sign_mod, wire
from skein import publish as pub_mod
from skein.ingress import ingest
from skein.station import Station
from skein.station_store import StationStore
from skein.envelope import folio_verdict

from tests import station_publish_helpers as h
from tests.station_publish_helpers import I, ALICE


def _unsigned_jwt(aud="sigstore", issuer="https://accounts.google.com") -> str:
    def b64(d):
        raw = json.dumps(d, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f'{b64({"alg": "none", "typ": "JWT"})}.{b64({"sub": "alice@example.com", "iss": issuer, "aud": aud})}.'


@pytest.fixture
def provider():
    return signing.OIDCProviderConfig(
        issuer="https://accounts.google.com", token=_unsigned_jwt(), provider_id="google"
    )


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein-next")
    yield s
    s.close()


def _ok_verifier(cb, b):
    return MultiVerifyResult(
        results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
        overall=VerifyStatus.VERIFIED)


def _signed_batch(signer=None):
    folios, threads, slugs = h.specs_set()
    batch = wire.build_batch(folios, threads, slugs)
    batch["manifest_signature"] = h.manifest_over(folios, threads, signer=signer)
    return batch


def _finding_hash(batch):
    return next(f["content_hash"] for f in batch["folios"] if f["type"] == "finding")


# --- E1: real signed publish through real signing.py -------------------------


def test_e2e_bound_signed_publish_accepted(instance, provider, monkeypatch):  # E1
    signing._test_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    signing._test_factory.install_verify_monkeypatch(monkeypatch)
    signer = sign_mod.make_oidc_signer(provider)
    # discover the verified identity, bind it on the instance
    probe = sign_mod.sign_manifest(["sha256::" + "0" * 64], signer)
    instance.store.add_binding(probe["issuer"], probe["subject"], role="originator")
    monkeypatch.setattr(
        pub_mod, "post_batch",
        lambda url, batch, timeout=30.0: ingest(instance, batch, require_signed=True),
    )
    folios, threads, slugs = h.specs_set()
    ack = pub_mod.publish("http://instance.example", folios, threads, signer, site_slugs=slugs)
    assert len(ack["accepted"]) == 2  # site folio + finding
    # read surface: SIGNED, attributed to the MANIFEST signer; verify_cache warmed
    ro = StationStore(db_path=instance.store.db_path, read_only=True)
    try:
        for h_ in ack["accepted"]:
            verdict, identity = folio_verdict(ro, h_, ro.get_folio(h_))
            assert verdict.startswith("SIGNED")
            assert identity["issuer"] == probe["issuer"]
    finally:
        ro.close()

    # The public wire carries the covering manifest proof and mesh verifies it
    # locally. This is the regression that the old mock-only mesh tests missed:
    # routing the manifest bundle through verify_wire_folio returned "wrong kind".
    from fastapi.testclient import TestClient
    from urllib.parse import urlparse

    from skein.mesh import client as mesh_client
    from skein.web.app import create_app as create_read_app

    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(instance.store.db_path.parent))
    monkeypatch.setenv("SKEIN_STATION_NAME", "roundtrip")
    read_client = TestClient(create_read_app())

    def read_get(url, timeout=None):
        parsed = urlparse(url)
        return read_client.get(parsed.path)

    monkeypatch.setattr(mesh_client.requests, "get", read_get)
    fetched = mesh_client.fetch("https://station.example", _finding_hash({"folios": folios}))
    assert fetched.exit_code == mesh_client.EXIT_OK
    assert fetched.state == "verified"
    assert fetched.identity["subject"] == probe["subject"]


# --- E2-E5: the gate logic end-to-end ---------------------------------------


def test_e2e_unsigned_publish_rejected(instance):  # E2
    folios, threads, slugs = h.specs_set()
    batch = wire.build_batch(folios, threads, slugs)  # no manifest
    ack = ingest(instance, batch, require_signed=True)
    assert all(r["reason"] == "no manifest" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_e2e_unbound_signer_rejected(instance):  # E3
    batch = _signed_batch()  # signer not bound on instance
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "unbound signer" for r in ack["rejected"])


def test_e2e_revoked_signer_rejected(instance):  # E4
    instance.store.add_binding(I, ALICE, role="originator")
    instance.store.revoke_binding(I, ALICE)
    batch = _signed_batch()
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "revoked binding" for r in ack["rejected"])


def test_e2e_membership_vs_no_manifest(instance, tmp_path):  # E5
    """A bound author's manifest-covered batch is accepted with per-constituent proofs;
    the same constituents delivered with NO manifest are rejected 'no manifest'."""
    instance.store.add_binding(I, ALICE, role="originator")
    batch = _signed_batch()
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert len(ack["accepted"]) == 2 and len(ack["threads"]["accepted"]) == 1
    for h_ in ack["accepted"] + ack["threads"]["accepted"]:
        proof = instance.store.get_constituent_proof(h_)
        assert proof["subject"] == ALICE
    # the same constituents with NO manifest -> 'no manifest'
    folios, threads, slugs = h.specs_set()
    bare = wire.build_batch(folios, threads, slugs)
    i2 = Station(tmp_path / "i2" / ".skein-next")
    try:
        ack2 = ingest(i2, bare, require_signed=True)
    finally:
        i2.close()
    assert all(r["reason"] == "no manifest" for r in ack2["rejected"])


def test_e2e_read_cache_hit_after_ingest(instance, monkeypatch):  # E6
    instance.store.add_binding(I, ALICE, role="originator")
    batch = _signed_batch()
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    finding = _finding_hash(batch)
    # second read hits verify_cache and does not re-run Sigstore
    ro = StationStore(db_path=instance.store.db_path, read_only=True)
    calls = []
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: calls.append(1) or _ok_verifier(cb, b))
    try:
        verdict, _ = folio_verdict(ro, finding, ro.get_folio(finding))
        assert verdict.startswith("SIGNED")
        assert calls == []  # warm cache elided Sigstore
    finally:
        ro.close()


# --- station-posture read visibility (the E11 analogue) ---------------------


def test_read_after_ingest_sees_commit(tmp_path):  # E11 (rollback-journal)
    inst = Station(tmp_path / ".skein-next")
    f = h.folio("finding", "T", "b", "2026-01-01T00:00:00+00:00")
    ingest(inst, {"protocol": wire.PROTOCOL, "folios": [wire.folio_to_wire(f)],
                  "threads": [], "site_slugs": {}}, require_signed=False)
    # a fresh read store opened mode=ro sees the committed write (no torn/stale read)
    ro = StationStore(db_path=inst.store.db_path, read_only=True)
    try:
        assert ro.get_folio(f["content_hash"]) is not None
    finally:
        ro.close()
        inst.close()
