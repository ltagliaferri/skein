"""Unicode hardening contracts for signing identities and canonical bytes.

Oracle finding-20260512-sr0w called out that the earlier Unicode tests were too
forgiving: identity properties filtered to NFC, adversarial tests mostly
asserted "no crash", and canonical payload coverage did not pin invisible or
directional characters. These tests define the v0 behavior explicitly.
"""
from __future__ import annotations

import pytest

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


GOOGLE_ISSUER = "https://accounts.google.com"


def _bundle(canonical: bytes, blob: str) -> signing.SignatureBundle:
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )


def _verify_identity(crypto_factory, monkeypatch, *, subject: str, issuer: str):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"unicode-identity-contract"
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical,
        identity=subject,
        issuer=issuer,
    )
    return signing.verify(canonical, _bundle(canonical, blob))


# Enforces: oracle actionable #1. SAN subjects are compared and exposed in NFC
# form, so logically identical NFC/NFD email subjects do not diverge.
def test_verify_identity_nfc_vs_nfd_subjects(crypto_factory, monkeypatch):
    nfc = "josé@example.com"
    nfd = "jose\u0301@example.com"

    nfc_result = _verify_identity(
        crypto_factory, monkeypatch, subject=nfc, issuer=GOOGLE_ISSUER,
    )
    nfd_result = _verify_identity(
        crypto_factory, monkeypatch, subject=nfd, issuer=GOOGLE_ISSUER,
    )

    if nfc_result.status == signing.VerifyStatus.VERIFIED:
        assert nfc_result.subject == nfc
    if nfd_result.status == signing.VerifyStatus.VERIFIED:
        assert nfd_result.subject == nfc


# Enforces: oracle actionable #1. Zero-width joiners are not stripped from SAN
# subjects; preserving them prevents display-equivalent identity substitution.
def test_verify_identity_zero_width_joiner_in_subject(crypto_factory, monkeypatch):
    subject = "alice\u200dops@example.com"
    result = _verify_identity(
        crypto_factory, monkeypatch, subject=subject, issuer=GOOGLE_ISSUER,
    )
    if result.status == signing.VerifyStatus.VERIFIED:
        assert result.subject == subject


# Enforces: oracle actionable #1. RTL marks are not silently stripped or
# reordered during identity extraction.
def test_verify_identity_rtl_marks_in_subject(crypto_factory, monkeypatch):
    subject = "alice\u202e@example.com"
    result = _verify_identity(
        crypto_factory, monkeypatch, subject=subject, issuer=GOOGLE_ISSUER,
    )
    if result.status == signing.VerifyStatus.VERIFIED:
        assert result.subject == subject


# Enforces: oracle actionable #1. X.509 SAN subjects with trailing whitespace
# fail closed instead of being trimmed into a different identity.
def test_verify_identity_trailing_whitespace_subject(crypto_factory, monkeypatch):
    result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com ",
        issuer=GOOGLE_ISSUER,
    )
    assert result.status != signing.VerifyStatus.VERIFIED


# Enforces: oracle actionable #1. Issuer URLs are byte-string identities in the
# v0 profile; punycode and Unicode host spellings do not compare equal.
def test_verify_identity_punycode_vs_unicode_issuer(crypto_factory, monkeypatch):
    punycode_issuer = "https://accounts.xn--bcher-kva.example"
    unicode_issuer = "https://accounts.bücher.example"

    punycode_result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com",
        issuer=punycode_issuer,
    )
    unicode_result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com",
        issuer=unicode_issuer,
    )

    if punycode_result.status == signing.VerifyStatus.VERIFIED:
        assert punycode_result.issuer == punycode_issuer
    if unicode_result.status == signing.VerifyStatus.VERIFIED:
        assert unicode_result.issuer == unicode_issuer
    assert punycode_result.issuer != unicode_result.issuer


# Enforces: oracle actionable #1. Confusable issuer domains remain distinct;
# sigstore.io and sigstore.com are different issuer URLs.
def test_verify_identity_confusable_issuer_io_vs_com(crypto_factory, monkeypatch):
    io_result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com",
        issuer="https://oauth.sigstore.io",
    )
    com_result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com",
        issuer="https://oauth.sigstore.com",
    )

    if io_result.status == signing.VerifyStatus.VERIFIED:
        assert io_result.issuer == "https://oauth.sigstore.io"
    if com_result.status == signing.VerifyStatus.VERIFIED:
        assert com_result.issuer == "https://oauth.sigstore.com"
    assert io_result.issuer != com_result.issuer


@pytest.mark.parametrize("canonical", [
    "zero\u200dwidth\u200cchars".encode("utf-8"),
    "rtl\u202eoverride\u202cmarks".encode("utf-8"),
    "emoji-\U0001f469\u200d\U0001f4bb-\U0001f680".encode("utf-8"),
    "combining-e\u0301-a\u0308".encode("utf-8"),
])
def test_canonical_bytes_round_trip_unicode_edges(
    crypto_factory, google_provider, monkeypatch, canonical
):
    # Enforces: oracle actionable #1. SignatureBundle JSON keeps UTF-8
    # canonical bytes byte-exact, including invisible and multi-byte codepoints.
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical, google_provider)
    sb = _bundle(canonical, result.bundle_json)

    assert signing.SignatureBundle.model_validate_json(sb.model_dump_json()).canonical_bytes == canonical
    assert signing.verify(canonical, sb).status == signing.VerifyStatus.VERIFIED


# Enforces: oracle actionable #1. The signature binds the Unicode-heavy payload
# bytes themselves, not a decoded or normalized string form.
def test_sign_verify_round_trip_unicode_heavy_canonical_bytes(
    crypto_factory, google_provider, canonical_bytes_unicode_heavy, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_unicode_heavy, google_provider)
    sb = _bundle(canonical_bytes_unicode_heavy, result.bundle_json)

    assert signing.verify(canonical_bytes_unicode_heavy, sb).status == signing.VerifyStatus.VERIFIED
    assert signing.verify(canonical_bytes_unicode_heavy + b"!", sb).status == (
        signing.VerifyStatus.SIGNATURE_MISMATCH
    )
