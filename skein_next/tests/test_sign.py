"""Signing at the publish boundary — wiring tests.

Two layers:
- A REAL round-trip through skein.signing.sign / verify_multi, driven offline by
  signing._test_factory (the same fake-at-the-sigstore-boundary machinery the
  signing suite uses). This proves the dormant primitive actually plugs into
  publish -> ingest -> verify-on-read.
- Fake signer/verifier unit tests for the rejection and policy branches, which
  don't need crypto.
"""

from __future__ import annotations

import base64
import json

import pytest

from skein import signing
from skein.signing import _test_factory

from skein_next.station import Station
from skein_next.ingress import ingest
from skein_next import publish as pub_mod
from skein_next import sign as sign_mod
from skein_next import canon, wire
from skein_next.web.adapter import ContentHashAdapter


def _unsigned_jwt(aud="sigstore", issuer="https://accounts.google.com") -> str:
    """A JWT-shaped token sign() can parse to enforce the aud allowlist."""
    def b64(d):
        raw = json.dumps(d, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f'{b64({"alg": "none", "typ": "JWT"})}.{b64({"sub": "alice@example.com", "iss": issuer, "aud": aud})}.'


@pytest.fixture
def client(tmp_path):
    s = Station(tmp_path / "client" / ".skein-next")
    yield s
    s.close()


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein-next")
    yield s
    s.close()


@pytest.fixture
def provider():
    return signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token=_unsigned_jwt(),
        provider_id="google",
    )


def _seed(station):
    station.create_site("specs", purpose="Public specs", created_by="t")
    return station.post("finding", "specs", "Design Overview", "rev-5 body", created_by="t")


# --- real round-trip --------------------------------------------------------


def test_sign_publish_verify_round_trip(client, instance, provider, monkeypatch):
    """Sign at publish, verify at ingest, verify on read — through real signing.py."""
    _test_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    _test_factory.install_verify_monkeypatch(monkeypatch)

    f1 = _seed(client)
    signer = sign_mod.make_oidc_signer(provider)

    # Loop the publish straight into the instance's ingest (no HTTP hop).
    monkeypatch.setattr(pub_mod, "post_batch", lambda url, batch, timeout=30.0: ingest(instance, batch))
    result = pub_mod.publish(client, "http://instance.example", site="specs", signer=signer)

    assert result["signed"] is True
    # Bundle landed on the instance AND was mirrored client-side.
    assert instance.store.get_signature(f1) is not None
    assert client.store.get_signature(f1) is not None
    # The signed folio verifies on the instance's read surface.
    adapter = ContentHashAdapter(instance.store.data_dir)
    try:
        prov = adapter.provenance(f1)
    finally:
        adapter.close()
    assert prov["signed"] is True
    assert prov["issuer"]  # the fake cert's issuer/subject flow through


def test_signature_bundle_is_overlay_not_identity(client, provider, monkeypatch):
    """Attaching a signature must not change the folio's content hash."""
    _test_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    f1 = _seed(client)
    wf = wire.folio_to_wire(client.store.get_folio(f1))
    signed = sign_mod.sign_wire_folio(wf, sign_mod.make_oidc_signer(provider))
    assert "signature_bundle" in signed
    assert wire.recompute_folio_hash(signed) == f1  # overlay ignored by the hash


# --- fake signer/verifier branches -----------------------------------------


def _fake_signer(canonical_bytes):
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1", bundles=["x"], canonical_bytes=canonical_bytes
    )


def _ok_verifier(canonical_bytes, bundle):
    return signing.MultiVerifyResult(
        results=[signing.VerifyResult(status=signing.VerifyStatus.VERIFIED, issuer="iss", subject="sub")],
        overall=signing.VerifyStatus.VERIFIED,
    )


def _bad_verifier(canonical_bytes, bundle):
    return signing.MultiVerifyResult(
        results=[signing.VerifyResult(status=signing.VerifyStatus.SIGNATURE_MISMATCH)],
        overall=signing.VerifyStatus.SIGNATURE_MISMATCH,
    )


def test_verify_wire_folio_unsigned_is_not_an_error(client):
    f1 = _seed(client)
    wf = wire.folio_to_wire(client.store.get_folio(f1))
    verified, reason, identity = sign_mod.verify_wire_folio(wf, _ok_verifier)
    assert verified is False and reason == "unsigned" and identity is None


def test_ingest_rejects_a_failing_signature(client, instance):
    f1 = _seed(client)
    folios, threads, slugs = pub_mod.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    batch["folios"] = [sign_mod.sign_wire_folio(wf, _fake_signer) for wf in batch["folios"]]

    ack = ingest(instance, batch, verifier=_bad_verifier)

    assert all(a == [] for a in (ack["accepted"], ack["existing"]))
    assert any(r["reason"].startswith("signature ") for r in ack["rejected"])
    assert instance.store.count_folios() == 0
    assert instance.store.get_signature(f1) is None


def test_ingest_stores_bundle_on_valid_signature(client, instance):
    _seed(client)
    folios, threads, slugs = pub_mod.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    batch["folios"] = [sign_mod.sign_wire_folio(wf, _fake_signer) for wf in batch["folios"]]

    ack = ingest(instance, batch, verifier=_ok_verifier)

    assert len(ack["accepted"]) == 2  # site folio + finding
    for h in ack["accepted"]:
        assert instance.store.get_signature(h) is not None


def test_require_signed_rejects_unsigned(client, instance):
    _seed(client)
    folios, threads, slugs = pub_mod.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)  # unsigned

    ack = ingest(instance, batch, require_signed=True)

    assert ack["accepted"] == []
    assert all(r["reason"] == "unsigned" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_require_signed_env_reaches_create_app(monkeypatch):
    from skein_next import ingress
    monkeypatch.setenv(ingress.ENV_REQUIRE_SIGNED, "1")
    assert ingress._require_signed() is True
    monkeypatch.setenv(ingress.ENV_REQUIRE_SIGNED, "no")
    assert ingress._require_signed() is False
    monkeypatch.delenv(ingress.ENV_REQUIRE_SIGNED, raising=False)
    assert ingress._require_signed() is False


def test_provenance_distinguishes_unverifiable_from_invalid(client, instance, monkeypatch):
    """A trust-root-unavailable verdict must read UNVERIFIED, not SIGNATURE INVALID."""
    _seed(client)
    folios, threads, slugs = pub_mod.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    batch["folios"] = [sign_mod.sign_wire_folio(wf, _fake_signer) for wf in batch["folios"]]
    ingest(instance, batch, verifier=_ok_verifier)  # store bundles

    f = next(f for f in folios if f["type"] == "finding")["content_hash"]
    adapter = ContentHashAdapter(instance.store.data_dir)

    def offline_verify(cb, bundle):
        return signing.MultiVerifyResult(
            results=[signing.VerifyResult(status=signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT)],
            overall=signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT,
        )
    # default_verifier calls signing.verify_multi at call time, so patch there.
    monkeypatch.setattr(signing, "verify_multi", offline_verify)
    try:
        prov = adapter.provenance(f)
    finally:
        adapter.close()
    assert prov["signed"] is False
    assert prov["signature_note"].startswith("UNVERIFIED")
    assert "INVALID" not in prov["signature_note"]
