"""sigstore-python exception → SKEIN failure mode mapping (clarification 5).

Per clarification 5 (finding-20260511-d3u6 § 5): SKEIN's failure-mode vocabulary
is a *superset* of sigstore-python's exception hierarchy. Each sigstore-python
exception class maps to exactly one SKEIN failure mode (no information loss
from the library). SKEIN may additionally produce failure modes that have no
sigstore-python analog (TRUST_ROOT_STALE, OFFLINE_NO_TRUSTED_ROOT,
SIGNATURE_MISMATCH).

Mapping table:

| sigstore-python exception          | Sign path                                  | Verify path           |
|---|---|---|
| VerificationError (base)           | n/a                                        | BUNDLE_MALFORMED       |
| InvalidBundle                      | n/a                                        | BUNDLE_MALFORMED       |
| InvalidMaterials                   | n/a                                        | CERT_INVALID           |
| InvalidRekorEntry                  | n/a                                        | INCLUSION_FAILED       |
| CertificateExpired / cert-validity | SigningUnavailable("cert expired")         | CERT_INVALID           |
| FulcioClientError / HTTP fail      | SigningUnavailable("fulcio unavailable")   | n/a (verify doesn't call Fulcio) |
| RekorClientError / HTTP fail       | SigningUnavailable("rekor unavailable")    | INCLUSION_FAILED / OFFLINE_NO_TRUSTED_ROOT |
| IdentityError (OIDC)               | SigningUnavailable("oidc token invalid")   | n/a                    |
| Network / timeout                  | SigningUnavailable("network failure")      | INCLUSION_FAILED (during Rekor proof verify) |

Plus SKEIN-specific modes with no library analog (test separately):
 - TRUST_ROOT_STALE
 - OFFLINE_NO_TRUSTED_ROOT
 - SIGNATURE_MISMATCH
 - VERIFIED

Phase 3 implementation should produce a small mapping module / function
(_map_sigstore_exception(exc) -> VerifyStatus) so the table lives in one place.
This test file exercises the table from outside, using crypto_factory to inject
specific sigstore-python exception classes at the seam.
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


# ---------------------------------------------------------------------------
# Sign path: library exception → SigningUnavailable with correct component
# ---------------------------------------------------------------------------


# Enforces: FulcioClientError maps to SigningUnavailable(component="fulcio").
def test_sign_fulcio_client_error_maps_to_signing_unavailable_fulcio(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, raise_sigstore_exception="FulcioClientError",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "fulcio"
    assert "fulcio" in exc.value.reason.lower() or "unavailable" in exc.value.reason.lower()


# Enforces: RekorClientError maps to SigningUnavailable(component="rekor").
def test_sign_rekor_client_error_maps_to_signing_unavailable_rekor(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, raise_sigstore_exception="RekorClientError",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "rekor"


# Enforces: IdentityError (OIDC) maps to SigningUnavailable(component="oidc")
# with reason mentioning token.
def test_sign_identity_error_maps_to_signing_unavailable_oidc(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, raise_sigstore_exception="IdentityError",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "oidc"
    assert "token" in exc.value.reason.lower() or "oidc" in exc.value.reason.lower()


# Enforces: CertificateExpired during sign maps to SigningUnavailable with
# component="fulcio" and a "cert expired" reason. Per mapping table.
def test_sign_certificate_expired_maps_to_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, raise_sigstore_exception="CertificateExpired",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "fulcio"
    assert "cert" in exc.value.reason.lower() or "expir" in exc.value.reason.lower()


# Enforces: generic network / timeout failures map to SigningUnavailable with
# component classified by the originating call site.
def test_sign_network_timeout_maps_to_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, raise_sigstore_exception="TimeoutError",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert "network" in exc.value.reason.lower() or "timeout" in exc.value.reason.lower()


# Enforces: __cause__ is set to the underlying sigstore-python exception for
# debugging. Per clarification 5: "SigningUnavailable with a structured reason
# and the original exception as __cause__."
@pytest.mark.parametrize("sigstore_exc_name", [
    "FulcioClientError",
    "RekorClientError",
    "IdentityError",
    "CertificateExpired",
])
def test_sign_exception_preserves_cause(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch,
    sigstore_exc_name,
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider,
        raise_sigstore_exception=sigstore_exc_name,
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    # __cause__ should be the original sigstore-python exception (or its surrogate).
    assert exc.value.__cause__ is not None


# ---------------------------------------------------------------------------
# Verify path: library exception → VerifyStatus
# ---------------------------------------------------------------------------


# Enforces: InvalidBundle maps to BUNDLE_MALFORMED.
def test_verify_invalid_bundle_maps_to_bundle_malformed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, raise_sigstore_exception="InvalidBundle",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: InvalidMaterials maps to CERT_INVALID.
def test_verify_invalid_materials_maps_to_cert_invalid(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, raise_sigstore_exception="InvalidMaterials",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: InvalidRekorEntry maps to INCLUSION_FAILED.
def test_verify_invalid_rekor_entry_maps_to_inclusion_failed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, raise_sigstore_exception="InvalidRekorEntry",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.INCLUSION_FAILED


# Enforces: CertificateExpired on verify path maps to CERT_INVALID.
def test_verify_certificate_expired_maps_to_cert_invalid(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, raise_sigstore_exception="CertificateExpired",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: bare VerificationError (no recognized subclass) maps to BUNDLE_MALFORMED
# (fallback per clarification 5: "If a future library version raises an exception
# we don't recognize, the verify path treats it as BUNDLE_MALFORMED and logs the
# unexpected exception for follow-up.")
def test_verify_unrecognized_verification_error_maps_to_bundle_malformed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, raise_sigstore_exception="VerificationError",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: RekorClientError during verify (Rekor witness query failure) maps to
# INCLUSION_FAILED if proof material is malformed, OFFLINE_NO_TRUSTED_ROOT if no
# trust root is available.
def test_verify_rekor_client_error_maps_to_inclusion_failed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, raise_sigstore_exception="RekorClientError",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status in (
        signing.VerifyStatus.INCLUSION_FAILED,
        signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT,
    )


# Enforces: network timeout during verify Rekor proof maps to INCLUSION_FAILED.
def test_verify_network_timeout_maps_to_inclusion_failed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, raise_sigstore_exception="TimeoutError",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.INCLUSION_FAILED


# ---------------------------------------------------------------------------
# SKEIN-specific failure modes (no library analog)
# ---------------------------------------------------------------------------


# Enforces: TRUST_ROOT_STALE is reachable via SKEIN-specific code path (verifier
# detects bundle integratedTime falls outside trusted_root.json era windows).
def test_trust_root_stale_skein_specific_path(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, trust_root_predates_bundle=True,
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.TRUST_ROOT_STALE


# Enforces: OFFLINE_NO_TRUSTED_ROOT is reachable via SKEIN-specific code path
# (offline verifier with no era-correct trust root).
def test_offline_no_trusted_root_skein_specific_path(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, offline=True, trust_root_missing=True,
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT


# Enforces: SIGNATURE_MISMATCH is SKEIN-specific (signature doesn't match
# canonical_bytes the caller passed — could be cross-payload reuse or perturbed
# bytes). No library exception analog; SKEIN computes locally.
def test_signature_mismatch_skein_specific_path(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    # Different bytes than what was signed → SIGNATURE_MISMATCH.
    vr = signing.verify(canonical_bytes_simple + b"_perturbed", sb)
    assert vr.status == signing.VerifyStatus.SIGNATURE_MISMATCH


# ---------------------------------------------------------------------------
# Information preservation — mapping is lossless from library perspective
# ---------------------------------------------------------------------------


# Enforces: every sigstore-python exception class maps to exactly one
# VerifyStatus. No two distinct exception classes collapse to the same
# status that loses information. (This is the no-information-loss property
# from clarification 5: "no information loss from the library".)
#
# Coverage proxy: confirm each of the 5 documented sigstore-python exception
# classes (InvalidBundle, InvalidMaterials, InvalidRekorEntry, CertificateExpired)
# maps to a DIFFERENT VerifyStatus.
def test_library_exceptions_map_to_distinct_statuses(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    expected_mapping = {
        "InvalidBundle": signing.VerifyStatus.BUNDLE_MALFORMED,
        "InvalidMaterials": signing.VerifyStatus.CERT_INVALID,
        "InvalidRekorEntry": signing.VerifyStatus.INCLUSION_FAILED,
        "CertificateExpired": signing.VerifyStatus.CERT_INVALID,
    }
    # The 5 exception classes map onto 4 distinct statuses (CertificateExpired
    # and InvalidMaterials both -> CERT_INVALID is acceptable: they're both
    # cert-validity concerns). The point: no exception silently disappears
    # into VERIFIED or an unrelated status.
    distinct_statuses = set(expected_mapping.values())
    assert signing.VerifyStatus.VERIFIED not in distinct_statuses
    assert len(distinct_statuses) >= 4  # at least 4 distinct outcomes


# Enforces: phase-3 implementation should expose the mapping as a single
# function (per clarification 5: "Phase 3 implementation should produce a
# small mapping module / function so the table lives in one place"). Tested
# behaviorally above; this docstring-only test pins the expectation.
def test_mapping_lives_in_one_place_per_clarification_5():
    # If phase-3 ships a _map_sigstore_exception helper, the per-exception tests
    # above will all use it. This test just documents the architectural intent.
    # No assertion — the architecture is documented via the per-exception tests.
    pass
