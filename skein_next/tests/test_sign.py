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

from skein_next import signing

from skein_next.station import Station
from skein_next import sign as sign_mod
from skein_next import canon, profile, wire


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


# NOTE (Phase 3 migration): the per-folio signing round-trip and overlay cells
# that lived here are dissolved with the per-folio signature path. Their coverage
# moves to the unified manifest model: the publish->ingest->read round-trip is the
# offline e2e (test_e2e_publish.py E1-E6); manifest acceptance/rejection is the RS
# table (test_require_signed.py); manifest signing/verification is SG/VM
# (test_verify_manifest.py).


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


@pytest.mark.parametrize("bad", [None, [], 7, "str", True])
def test_verify_wire_folio_hostile_shape_is_typed_reject_not_raise(bad):
    # A non-mapping wire_folio (incl. None) must not hit wire_folio.get() and
    # raise AttributeError — it's a typed reject, matching every other
    # verify_wire_* totality guard.
    verified, reason, identity = sign_mod.verify_wire_folio(bad, _ok_verifier)
    assert (verified, reason, identity) == (False, "invalid fields", None)


def test_verify_wire_folio_bad_field_type_is_typed_reject_before_crypto():
    # title=True is signed-looking (carries a parseable signature_bundle) but
    # canon.folio_canonical_bytes raises CanonError on a non-str/non-None
    # scalar field. That must surface as a typed reject, not an unhandled raise,
    # and the verifier must never run.
    bundle = _fake_signer(b"irrelevant-never-reached")
    wf = {
        "type": "finding",
        "title": True,
        "content": "body",
        "created_at": "2026-01-01T00:00:00Z",
        "created_by": "a",
        "signature_bundle": bundle.model_dump_json(),
    }
    v, seen = _capturing_ok()
    verified, reason, identity = sign_mod.verify_wire_folio(wf, v)
    assert (verified, reason, identity) == (False, "invalid fields", None)
    assert "bytes" not in seen  # crypto never ran


def test_verify_wire_folio_unparseable_created_at_is_typed_reject_before_crypto():
    # An unparseable created_at raises ValueError inside canon; must also be a
    # typed reject rather than an unhandled raise.
    bundle = _fake_signer(b"irrelevant-never-reached")
    wf = {
        "type": "finding",
        "title": "T",
        "content": "body",
        "created_at": "not-a-date",
        "created_by": "a",
        "signature_bundle": bundle.model_dump_json(),
    }
    v, seen = _capturing_ok()
    verified, reason, identity = sign_mod.verify_wire_folio(wf, v)
    assert (verified, reason, identity) == (False, "invalid fields", None)
    assert "bytes" not in seen  # crypto never ran


def test_verify_multi_empty_bundle_list_is_bundle_malformed_not_raise():
    # SignatureBundle's own validator raises the custom EmptySignatureBundle
    # (not a ValueError/ValidationError) for bundles=[]; verify_multi is a
    # wire-facing entrypoint that must fail closed on that, not propagate it.
    result = signing.verify_multi(
        b"x",
        {
            "identity_scheme": "sigstore-public-v1",
            "bundles": [],
            "canonical_bytes": b"x",
            "canon_version": "skein.folio.canon/v1",
        },
    )
    assert result.overall == signing.VerifyStatus.BUNDLE_MALFORMED
    assert result.results == [signing.VerifyResult(status=signing.VerifyStatus.BUNDLE_MALFORMED)]


def test_require_signed_env_reaches_create_app(monkeypatch):
    from skein_next import ingress

    monkeypatch.setenv(ingress.ENV_REQUIRE_SIGNED, "1")
    assert ingress._require_signed() is True
    monkeypatch.setenv(ingress.ENV_REQUIRE_SIGNED, "no")
    assert ingress._require_signed() is False
    monkeypatch.delenv(ingress.ENV_REQUIRE_SIGNED, raising=False)
    assert ingress._require_signed() is False


def test_require_signed_recognizes_wider_truthy_and_falsy_spellings(monkeypatch):  # finding-8
    from skein_next import ingress

    for truthy in ("1", "true", "yes", "on", "enabled", "y", "TRUE", "On", " yes "):
        monkeypatch.setenv(ingress.ENV_REQUIRE_SIGNED, truthy)
        assert ingress._require_signed() is True, truthy
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(ingress.ENV_REQUIRE_SIGNED, falsy)
        assert ingress._require_signed() is False, falsy


def test_require_signed_unrecognized_value_fails_loud_not_open(monkeypatch):  # finding-8
    """A plausible-but-unrecognized spelling (a typo, or a value from some other
    tool's boolean convention) must never be silently treated as OFF — that would
    boot the public ingress accepting unsigned content with only a log line. It
    must raise instead, at the exact call site create_app() uses to decide the
    startup posture."""
    from skein_next import ingress

    for garbage in ("onn", "enable", "2", "truthy", "nope"):
        monkeypatch.setenv(ingress.ENV_REQUIRE_SIGNED, garbage)
        with pytest.raises(ingress.RequireSignedConfigError):
            ingress._require_signed()


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


# test_provenance_distinguishes_unverifiable_from_invalid migrated to the manifest
# read path (test_envelope.py, the folio_verdict-over-manifest cells).
