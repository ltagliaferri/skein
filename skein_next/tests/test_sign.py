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
from skein_next import canon, profile, wire
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
    monkeypatch.setattr(
        pub_mod, "post_batch", lambda url, batch, timeout=30.0: ingest(instance, batch)
    )
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
    # Mirror the real signer's domain separation: sign the profiled preimage and
    # stamp the profile as canon_version, so the strict verifier's profile gate
    # passes and the fake verifier branches are what's under test.
    preimage = profile.profiled_preimage(profile.CANON_PROFILE_V1, canonical_bytes)
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["x"],
        canonical_bytes=preimage,
        canon_version=profile.CANON_PROFILE_V1,
    )


def _ok_verifier(canonical_bytes, bundle):
    return signing.MultiVerifyResult(
        results=[
            signing.VerifyResult(status=signing.VerifyStatus.VERIFIED, issuer="iss", subject="sub")
        ],
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


# --- strict verification path (ujwx §4) -------------------------------------

from skein_next.identity import compute_folio_hash  # noqa: E402

_FIELDS = {
    "type": "finding",
    "title": "T",
    "content": "body",
    "created_at": "2026-01-01T00:00:00Z",
    "created_by": "a",
}


def _signed_wf(fields=_FIELDS):
    ch = compute_folio_hash(fields)
    wf = {**fields, "content_hash": ch}
    bundle = _fake_signer(canon.folio_canonical_bytes(wf))
    wf["signature_bundle"] = bundle.model_dump_json()
    return wf


def _capturing_ok():
    seen = {}

    def v(canonical_bytes, bundle):
        seen["bytes"] = canonical_bytes
        return signing.MultiVerifyResult(
            results=[
                signing.VerifyResult(status=signing.VerifyStatus.VERIFIED, issuer="i", subject="s")
            ],
            overall=signing.VerifyStatus.VERIFIED,
        )

    return v, seen


def test_strict_hash_mismatch_short_circuits_before_crypto():
    # A tampered body (same claimed content_hash) is rejected at step 1; the
    # verifier is never consulted.
    wf = _signed_wf()
    wf["title"] = "TAMPERED"  # content_hash no longer matches the body
    v, seen = _capturing_ok()
    verified, reason, identity = sign_mod.verify_wire_folio(wf, v)
    assert verified is False and reason == "hash mismatch" and identity is None
    assert "bytes" not in seen  # crypto never ran


def test_strict_unknown_profile_is_a_hard_fail_before_crypto():
    # An old raw-v0 bundle (canon_version="knurl-1.0") is not in the registry.
    fields = _FIELDS
    ch = compute_folio_hash(fields)
    raw_bundle = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["x"],
        canonical_bytes=canon.folio_canonical_bytes(fields),
        canon_version="knurl-1.0",
    )
    wf = {**fields, "content_hash": ch, "signature_bundle": raw_bundle.model_dump_json()}
    v, seen = _capturing_ok()
    verified, reason, _ = sign_mod.verify_wire_folio(wf, v)
    assert verified is False and reason == "unknown profile"
    assert "bytes" not in seen  # never downgraded to verify the raw signature


def test_strict_verifies_over_the_domain_separated_preimage():
    wf = _signed_wf()
    v, seen = _capturing_ok()
    verified, reason, identity = sign_mod.verify_wire_folio(wf, v)
    assert verified is True and reason == "verified"
    # the bytes handed to the verifier are profile || NUL || canonical_bytes,
    # not the bare canonical bytes
    expected = profile.profiled_preimage(profile.CANON_PROFILE_V1, canon.folio_canonical_bytes(wf))
    assert seen["bytes"] == expected
    assert seen["bytes"] != canon.folio_canonical_bytes(wf)


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


def test_acquire_oidc_provider_extracts_issuer_and_token(monkeypatch):
    """The login flow's IdentityToken -> OIDCProviderConfig mapping (no browser)."""
    captured = {}

    class FakeToken:
        issuer = "https://oauth2.sigstore.dev/auth"

        def __str__(self):
            return "raw.jwt.token"

    class FakeIssuer:
        def __init__(self, base):
            captured["base"] = base

        def identity_token(self, force_oob=False):
            captured["force_oob"] = force_oob
            return FakeToken()

    monkeypatch.setattr("sigstore.oidc.Issuer", FakeIssuer)
    prov = sign_mod.acquire_oidc_provider(force_oob=True)
    assert prov.issuer == "https://oauth2.sigstore.dev/auth"  # allowlisted broker
    assert prov.token == "raw.jwt.token"
    assert captured["base"] == sign_mod.SIGSTORE_PROD_ISSUER  # production only for v0
    assert captured["force_oob"] is True


def test_build_signer_login_path_uses_acquire(monkeypatch, provider):
    """CLI --sign --login builds a signer from the interactive flow."""
    from skein_next import cli

    monkeypatch.setattr(sign_mod, "acquire_oidc_provider", lambda **kw: provider)
    signer = cli._build_signer("x", None, login=True)
    assert callable(signer)


def test_publish_signing_flag_guards(tmp_path):
    """Signing flags without --sign, and --login+--oidc-token together, are rejected."""
    from click.testing import CliRunner
    from skein_next.cli import cli

    runner = CliRunner()
    dd = ["--data-dir", str(tmp_path / "x")]

    no_sign = runner.invoke(cli, [*dd, "publish", "--site", "s", "--to", "http://h", "--login"])
    assert no_sign.exit_code != 0 and "only apply with --sign" in no_sign.output

    both = runner.invoke(
        cli,
        [
            *dd,
            "publish",
            "--site",
            "s",
            "--to",
            "http://h",
            "--sign",
            "--login",
            "--oidc-token",
            "t",
        ],
    )
    assert both.exit_code != 0 and "not both" in both.output


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
