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


class TestExtractSubjectEmptySanRejected:
    """Empty SAN must not propagate through _extract_subject_from_cert
    as the empty string.

    _decode_der_utf8(bytes([0x0c, 0x00])) is a syntactically valid DER
    encoding of a zero-length UTF8String — it returns ''. Before the
    fix, _extract_subject_from_cert returned that '' as the subject and
    verify()'s `if subject is None` guard let it through; the
    VerifyResult surfaced VERIFIED with subject=''. Empty subject is
    not a usable identity — any caller comparing result.subject against
    an expected signer would be misled. The fix rejects empty raw at
    the extraction layer (returning None so verify() maps to
    CERT_INVALID) and uses a truthiness check at the verify() guard.

    These tests stub cert objects directly rather than going through
    the cryptography library's normal cert builder. The library refuses
    to construct an empty x509.RFC822Name (raises ValueError), but it
    DOES accept an empty x509.UniformResourceIdentifier, so both the
    URI and OtherName-UTF8String paths can deliver an empty raw to
    _extract_subject_from_cert. Both are covered below.
    """

    def _stub_cert_with_san(self, san_objects):
        from unittest.mock import Mock
        san_ext_value = Mock()
        san_ext_value.__iter__ = lambda self: iter(san_objects)
        san_ext = Mock()
        san_ext.value = san_ext_value
        cert = Mock()
        cert.extensions.get_extension_for_class.return_value = san_ext
        return cert

    def test_empty_othername_utf8string_returns_none(self):
        from cryptography import x509
        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        # 0x0c 0x00 = DER UTF8String, length 0 -> decodes to ''
        empty_other = x509.OtherName(type_id=other_oid, value=bytes([0x0C, 0x00]))
        cert = self._stub_cert_with_san([empty_other])
        assert signing._extract_subject_from_cert(cert) is None

    def test_whitespace_only_othername_returns_none(self):
        from cryptography import x509
        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        # 0x0c 0x01 0x20 = DER UTF8String containing a single space.
        ws_other = x509.OtherName(type_id=other_oid, value=bytes([0x0C, 0x01, 0x20]))
        cert = self._stub_cert_with_san([ws_other])
        # `raw != raw.strip()` catches this — pre-existing path; pin the
        # behavior so the empty-string fix doesn't accidentally weaken it.
        assert signing._extract_subject_from_cert(cert) is None

    def test_valid_othername_unchanged(self):
        from cryptography import x509
        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        # 0x0c 0x05 "alice"
        valid_other = x509.OtherName(
            type_id=other_oid, value=bytes([0x0C, 0x05]) + b"alice",
        )
        cert = self._stub_cert_with_san([valid_other])
        assert signing._extract_subject_from_cert(cert) == "alice"

    def test_empty_uri_san_returns_none(self):
        # x509.UniformResourceIdentifier('') constructs without error
        # (unlike RFC822Name, which rejects empty at construction). An
        # empty URI SAN therefore reaches _extract_subject_from_cert as
        # raw=''. The same `if not raw: return None` guard that catches
        # the OtherName empty case must catch this too.
        from cryptography import x509
        empty_uri = x509.UniformResourceIdentifier("")
        cert = self._stub_cert_with_san([empty_uri])
        assert signing._extract_subject_from_cert(cert) is None

    def test_othername_with_wrong_der_tag_returns_none(self):
        # Prior implementation fell back to `name.value.decode("utf-8")`
        # when _decode_der_utf8 rejected the value. b"\x16\x05alice"
        # (IA5String tag 0x16 + length 5 + "alice") would round-trip as
        # the identity string "\x16\x05alice" — leading control chars
        # slipping past the NUL / strip / NFC guards because \x16 (SYN)
        # is not in str.strip()'s default whitespace set.
        from cryptography import x509
        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        ia5_other = x509.OtherName(type_id=other_oid, value=b"\x16\x05alice")
        cert = self._stub_cert_with_san([ia5_other])
        assert signing._extract_subject_from_cert(cert) is None

    def test_othername_with_garbage_bytes_returns_none(self):
        # Truly arbitrary bytes that happen to be valid UTF-8 but are
        # not a DER UTF8String. Prior implementation would have returned
        # this garbage as the subject identity.
        from cryptography import x509
        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        garbage_other = x509.OtherName(type_id=other_oid, value=b"\x16\x0cadmin@corp.com")
        cert = self._stub_cert_with_san([garbage_other])
        assert signing._extract_subject_from_cert(cert) is None


class TestExtractIssuerV2DerStrict:
    """Issuer V2 (OID 1.3.6.1.4.1.57264.1.8) extraction must use strict DER.

    Prior implementation had a raw-UTF-8 fallback that admitted any byte
    sequence that happened to be valid UTF-8 — including IA5String-tagged
    values where the leading tag/length bytes become control characters
    embedded in the returned issuer string. The Issuer V2 path also
    skips the NUL/strip/NFC post-extraction guards entirely (it returns
    the decoded value directly), so the V2 raw fallback was an even
    sharper attack surface than the subject path.
    """

    def _stub_cert_with_issuer(self, oid_str, issuer_bytes):
        from unittest.mock import Mock
        ext = Mock()
        ext.oid.dotted_string = oid_str
        ext.value.value = issuer_bytes
        cert = Mock()
        cert.extensions = [ext]
        return cert

    def test_wrong_tag_v2_returns_none(self):
        # IA5String tag where DER UTF8String was expected.
        cert = self._stub_cert_with_issuer(
            "1.3.6.1.4.1.57264.1.8", b"\x16\x05alice",
        )
        assert signing._extract_issuer_from_cert(cert) is None

    def test_garbage_v2_returns_none(self):
        # Arbitrary valid UTF-8 bytes that are not DER UTF8String. Prior
        # implementation returned these as the issuer string.
        cert = self._stub_cert_with_issuer(
            "1.3.6.1.4.1.57264.1.8", b"\x16\x1bhttps://accounts.google.com",
        )
        assert signing._extract_issuer_from_cert(cert) is None

    def test_valid_v2_returns_decoded(self):
        # Regression: a real Fulcio-shaped DER UTF8String issuer round-trips.
        issuer = "https://accounts.google.com"
        body = issuer.encode("utf-8")
        # Short-form DER: 0x0C, length, body.
        der = bytes([0x0C, len(body)]) + body
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.8", der)
        assert signing._extract_issuer_from_cert(cert) == issuer

    def test_legacy_path_still_uses_raw_utf8(self):
        # The legacy issuer OID (1.3.6.1.4.1.57264.1.1) IS spec'd as raw
        # UTF-8 bytes (no DER wrapping); preserve that path.
        cert = self._stub_cert_with_issuer(
            "1.3.6.1.4.1.57264.1.1", b"https://accounts.google.com",
        )
        assert signing._extract_issuer_from_cert(cert) == "https://accounts.google.com"


class TestDecodeDerUtf8Strict:
    """White-box on _decode_der_utf8: strict DER UTF8String parse.

    The function powers issuer/subject extraction from Fulcio cert extensions
    (OID 1.3.6.1.4.1.57264.1.8 issuer V2 and OID 1.3.6.1.4.1.57264.1.24
    signer-identity OtherName). Pre-tightening, it accepted BER
    indefinite-length encoding (data[1] == 0x80, returned empty string) and
    silently truncated bodies whose claimed length exceeded the buffer
    (returned partial bytes). Both shapes are now rejected per X.690.
    """

    def test_happy_path_short_form(self):
        assert signing._decode_der_utf8(bytes([0x0C, 0x05]) + b"hello") == "hello"
        assert signing._decode_der_utf8(bytes([0x0C, 0x00])) == ""

    def test_happy_path_long_form_128(self):
        # Length 128 — smallest legal long-form encoding.
        data = bytes([0x0C, 0x81, 0x80]) + (b"X" * 128)
        assert signing._decode_der_utf8(data) == "X" * 128

    def test_wrong_tag_rejected(self):
        assert signing._decode_der_utf8(bytes([0x0D, 0x01, 0x41])) is None
        assert signing._decode_der_utf8(b"") is None
        assert signing._decode_der_utf8(b"\x0C") is None

    def test_ber_indefinite_length_rejected(self):
        # 0x0C 0x80 -> BER indefinite-length, illegal in DER (X.690 §10.1).
        # Was decoded as empty string under lenient parser.
        assert signing._decode_der_utf8(bytes([0x0C, 0x80])) is None

    def test_silent_truncation_rejected(self):
        # Claims length 10, supplies only 5. Was decoded as partial 'hello'.
        assert (
            signing._decode_der_utf8(bytes([0x0C, 0x0A]) + b"hello") is None
        )
        # Claims length 200, supplies 50.
        assert (
            signing._decode_der_utf8(bytes([0x0C, 0x81, 0xC8]) + b"X" * 50)
            is None
        )

    def test_non_minimal_long_form_rejected(self):
        # Length < 128 must use short form (X.690 §10.1).
        assert (
            signing._decode_der_utf8(bytes([0x0C, 0x81, 0x7F]) + b"X" * 127)
            is None
        )
        assert (
            signing._decode_der_utf8(bytes([0x0C, 0x81, 0x05]) + b"hello") is None
        )

    def test_leading_zero_in_long_form_length_rejected(self):
        # Leading zero octet would not be minimal — X.690 §10.1 forbids it.
        assert (
            signing._decode_der_utf8(bytes([0x0C, 0x82, 0x00, 0x80]) + b"X" * 128)
            is None
        )

    def test_trailing_garbage_rejected(self):
        # The encoded value must consume exactly the input — any trailing
        # bytes mean the input is not a single well-formed UTF8String.
        assert (
            signing._decode_der_utf8(bytes([0x0C, 0x05]) + b"hello" + b"EXTRA")
            is None
        )

    def test_insufficient_length_octets_rejected(self):
        # 0x82 says "2 length octets follow", but only 1 is present.
        assert signing._decode_der_utf8(bytes([0x0C, 0x82, 0x00])) is None

    def test_invalid_utf8_body_rejected(self):
        # 0xFF is never a valid UTF-8 start byte.
        assert signing._decode_der_utf8(bytes([0x0C, 0x01, 0xFF])) is None
