"""SAN extraction tests for verify() identity extraction policy."""
from __future__ import annotations

import pytest

pytest.importorskip("skein.signing")

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


def _sb(canonical: bytes, blob: str) -> signing.SignatureBundle:
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )


def test_verify_extracts_rfc822name_san(crypto_factory, monkeypatch):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-rfc822"
    value = "alice@example.com"
    blob = crypto_factory.make_bundle_blob_with_san(
        san_type="rfc822Name", value=value,
        canonical_bytes=canonical, identity=value, issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == value


def test_verify_extracts_uri_san(crypto_factory, monkeypatch):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-uri"
    value = "https://example.com/identity/alice"
    blob = crypto_factory.make_bundle_blob_with_san(
        san_type="uniformResourceIdentifier", value=value,
        canonical_bytes=canonical, identity=value, issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == value


def test_verify_extracts_othername_oid_57264_1_24(crypto_factory, monkeypatch):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-othername"
    value = "repo:owner/name:ref:refs/heads/main"
    blob = crypto_factory.make_bundle_blob_with_san(
        san_type="otherName_oid_57264_1_24", value=value,
        canonical_bytes=canonical, identity=value, issuer="https://token.actions.githubusercontent.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == value


def test_verify_multiple_sans_extraction_policy(crypto_factory, monkeypatch):
    # Policy pin per finding-20260514-burb (closes brief-20260514-cw13):
    # multi-SAN preference order is rfc822Name → uniformResourceIdentifier →
    # otherName OID 1.3.6.1.4.1.57264.1.24. This test covers the rfc822Name-vs-URI
    # edge; the URI-vs-otherName and three-way edges are covered by the two
    # tests below.
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-multi"
    blob = crypto_factory.make_bundle_blob_with_multiple_sans(
        sans=[("uniformResourceIdentifier", "https://example.com/u/alice"), ("rfc822Name", "alice@example.com")],
        canonical_bytes=canonical, identity="alice@example.com", issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == "alice@example.com"


def test_verify_multi_sans_uri_outranks_othername_oid(crypto_factory, monkeypatch):
    # Enforces: uniformResourceIdentifier outranks otherName OID
    # 1.3.6.1.4.1.57264.1.24. Second preference edge of the policy ratified
    # by finding-20260514-burb. Fact-pattern is non-hypothetical: Fulcio CI
    # OIDC issues URI + otherName multi-SAN certs.
    #
    # otherName value mirrors the Fulcio CI signer-identity shape used by
    # the single-SAN test at test_verify_extracts_othername_oid_57264_1_24
    # so the fixture matches real Fulcio wire content.
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-uri-vs-othername"
    uri_value = "https://github.com/owner/repo/.github/workflows/ci.yml@refs/heads/main"
    othername_value = "repo:owner/repo:ref:refs/heads/main"
    blob = crypto_factory.make_bundle_blob_with_multiple_sans(
        sans=[
            ("otherName_oid_57264_1_24", othername_value),
            ("uniformResourceIdentifier", uri_value),
        ],
        canonical_bytes=canonical, identity=uri_value,
        issuer="https://token.actions.githubusercontent.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == uri_value, (
        f"URI must outrank otherName per finding-20260514-burb; got {vr.subject!r}"
    )


def test_verify_multi_sans_three_way_rfc822_wins(crypto_factory, monkeypatch):
    # Enforces: when all three SAN types coexist on the same cert,
    # rfc822Name wins (top of the preference order). Closes the transitivity
    # gap on the SAN policy from finding-20260514-burb — a non-transitive
    # implementation that prefers rfc822 > URI and URI > otherName but
    # surfaces otherName when all three are present would pass the pairwise
    # tests but fail here.
    #
    # otherName value mirrors the Fulcio CI signer-identity shape.
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-three-way"
    rfc822 = "alice@example.com"
    uri = "https://example.com/u/alice"
    othername = "repo:alice/site:ref:refs/heads/main"
    blob = crypto_factory.make_bundle_blob_with_multiple_sans(
        sans=[
            ("otherName_oid_57264_1_24", othername),
            ("uniformResourceIdentifier", uri),
            ("rfc822Name", rfc822),
        ],
        canonical_bytes=canonical, identity=rfc822, issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == rfc822, (
        f"rfc822Name must win in three-way coexistence per finding-20260514-burb; "
        f"got {vr.subject!r}"
    )


def test_verify_missing_san_returns_cert_invalid(crypto_factory, monkeypatch):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-missing"
    blob = crypto_factory.make_bundle_blob_with_missing_san(
        canonical_bytes=canonical, identity="alice@example.com", issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.CERT_INVALID


def test_verify_malformed_ia5string_san_returns_cert_invalid(crypto_factory, monkeypatch):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-malformed"
    blob = crypto_factory.make_bundle_blob_with_malformed_ia5string_san(
        canonical_bytes=canonical, identity="alice@example.com", issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.CERT_INVALID
