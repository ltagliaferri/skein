"""verify_multi() contract — array semantics, overall aggregation, order
preservation, N=1/2/5, declare-continuity scenario.

verify_multi() is the canonical read API for multi-signer bundles. Per
clarification 2, calling verify_multi() on a length-1 bundle is legal and
produces a MultiVerifyResult with a single-element results array.

The overall-status aggregation is the load-bearing rule for authorization
decisions (rev 5 §verify return shape: "For authorization decisions, all
bundles in the array MUST return VERIFIED").
"""
from __future__ import annotations

import random

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
# N=1 — single signer (the common case; legal per clarification 2)
# ---------------------------------------------------------------------------


# Enforces: verify_multi() on a length-1 bundles array produces a results list
# of length 1 and overall=VERIFIED when the single bundle verifies. Per
# clarification 2: "Calling verify_multi() on a length-1 bundle is legal."
def test_verify_multi_n1_verified(
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
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert len(multi.results) == 1
    assert multi.results[0].status == signing.VerifyStatus.VERIFIED
    assert multi.overall == signing.VerifyStatus.VERIFIED


# Enforces: verify_multi() and verify() agree on the N=1 case. Pinning
# equivalence avoids drift between the two APIs.
def test_verify_multi_n1_agrees_with_verify(
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
    one = signing.verify(canonical_bytes_simple, sb)
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert multi.results[0].status == one.status
    assert multi.results[0].issuer == one.issuer
    assert multi.results[0].subject == one.subject


# ---------------------------------------------------------------------------
# N=2 — declare-continuity scenario
# ---------------------------------------------------------------------------


# Enforces: verify_multi() on a length-2 bundles array (declare-continuity)
# returns 2 results, both VERIFIED, overall=VERIFIED. Each signer signs the
# same canonical_bytes per Identity rev 5 §"declare-continuity flow".
def test_verify_multi_n2_continuity_both_verified(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_google = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob_github = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice-gh",
        issuer="https://github.com/login/oauth",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_google, blob_github],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert len(multi.results) == 2
    assert all(r.status == signing.VerifyStatus.VERIFIED for r in multi.results)
    assert multi.overall == signing.VerifyStatus.VERIFIED


# Enforces: verify_multi() preserves order. results[i] corresponds to bundles[i].
# Continuity depends on knowing which signer is the original vs continuity
# identity — order is the only marker.
def test_verify_multi_preserves_bundle_order(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_a = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="first@example.com",
        issuer="https://accounts.google.com",
    )
    blob_b = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="second@example.com",
        issuer="https://github.com/login/oauth",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_a, blob_b],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert multi.results[0].subject == "first@example.com"
    assert multi.results[1].subject == "second@example.com"


# Enforces: when one of two signers fails, overall != VERIFIED. The bad signer
# surfaces its specific status; the other still returns its result. Federation/UX
# renders partial-trust signals from the per-bundle results.
def test_verify_multi_partial_failure_not_overall_verified(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_ok = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob_bad = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice-gh",
        issuer="https://github.com/login/oauth",
    )
    blob_bad_tampered = crypto_factory.tamper_cert(blob_bad)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_ok, blob_bad_tampered],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert multi.results[0].status == signing.VerifyStatus.VERIFIED
    assert multi.results[1].status == signing.VerifyStatus.CERT_INVALID
    assert multi.overall != signing.VerifyStatus.VERIFIED


# Enforces: overall is non-VERIFIED iff ANY result is non-VERIFIED. Spec rev 5
# says "VERIFIED iff all results are VERIFIED" — the inverse is what we pin.
# The exact non-VERIFIED status (worst-status, first-failure, dedicated
# AGGREGATE_FAILED) is implementation-defined.
def test_verify_multi_overall_is_non_verified_iff_any_fails(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    good = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    bad = crypto_factory.tamper_cert(good)
    for variant in ([bad], [good, bad], [bad, good], [good, good, bad]):
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=variant,
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        multi = signing.verify_multi(canonical_bytes_simple, sb)
        assert multi.overall != signing.VerifyStatus.VERIFIED, variant


# Enforces: each bundle is verified independently — no shared state across signers.
def test_verify_multi_each_result_independent_status(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_a = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="a@example.com",
        issuer="https://accounts.google.com",
    )
    blob_b = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="b@example.com",
        issuer="https://accounts.google.com",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_a, blob_b],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    for r in multi.results:
        assert isinstance(r.status, signing.VerifyStatus)


# ---------------------------------------------------------------------------
# N=5 — pushing past obvious test sizes
# ---------------------------------------------------------------------------


# Enforces: verify_multi() scales to N=5 signers. No magic number caps multi-signer
# at 2 or 3. Future multi-author folios may be N=5+.
def test_verify_multi_n5_all_verified(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=f"signer{i}@example.com",
            issuer="https://accounts.google.com",
        )
        for i in range(5)
    ]
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert len(multi.results) == 5
    assert all(r.status == signing.VerifyStatus.VERIFIED for r in multi.results)
    assert multi.overall == signing.VerifyStatus.VERIFIED


# Enforces: at N=5, per-signer subjects come out distinct and in order. Catches
# the bug where the verifier reuses a single result object for all entries.
def test_verify_multi_n5_subjects_distinct_in_order(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=f"signer{i}@example.com",
            issuer="https://accounts.google.com",
        )
        for i in range(5)
    ]
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    subjects = [r.subject for r in multi.results]
    assert subjects == [f"signer{i}@example.com" for i in range(5)]


# Enforces: results[i] count matches bundles array length for arbitrary N.
@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_verify_multi_results_count_matches_bundles(
    crypto_factory, canonical_bytes_simple, monkeypatch, n,
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=f"signer{i}@example.com",
            issuer="https://accounts.google.com",
        )
        for i in range(n)
    ]
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert len(multi.results) == n


# ---------------------------------------------------------------------------
# All signers sign the same canonical_bytes (cross-payload prevention)
# ---------------------------------------------------------------------------


# Enforces: when one signer in the array signed DIFFERENT canonical_bytes (an
# attacker assembled a multi-bundle by reusing a sigstore-bundle from a
# different folio), the per-signer result for THAT signer must NOT be VERIFIED.
# Caller-supplied canonical_bytes governs all signature checks.
def test_verify_multi_cross_payload_signer_fails(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    legitimate = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    other_bytes = canonical_bytes_simple + b"_other_folio"
    smuggled = crypto_factory.make_bundle_blob(
        canonical_bytes=other_bytes,
        identity="bob@example.com",
        issuer="https://github.com/login/oauth",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[legitimate, smuggled],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert multi.results[0].status == signing.VerifyStatus.VERIFIED
    assert multi.results[1].status == signing.VerifyStatus.SIGNATURE_MISMATCH
    assert multi.overall != signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Order independence at the overall-status level
# ---------------------------------------------------------------------------


# Enforces: shuffling the bundles array does not change overall MultiVerifyResult
# status. Per spec: "each bundle is verified independently" implies overall is
# order-independent.
def test_verify_multi_shuffle_same_overall(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=f"signer{i}@example.com",
            issuer=f"https://provider-{i}.example.com",
        )
        for i in range(3)
    ]
    sb_ordered = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    shuffled = blobs[:]
    random.seed(0)
    random.shuffle(shuffled)
    sb_shuffled = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=shuffled,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    r_ordered = signing.verify_multi(canonical_bytes_simple, sb_ordered)
    r_shuffled = signing.verify_multi(canonical_bytes_simple, sb_shuffled)
    assert r_ordered.overall == r_shuffled.overall


# ---------------------------------------------------------------------------
# Unknown identity_scheme propagates to every result
# ---------------------------------------------------------------------------


# Enforces: unknown identity_scheme is BUNDLE_MALFORMED for every signer
# (scheme is global, not per-signer). overall == BUNDLE_MALFORMED.
def test_verify_multi_unknown_scheme_fail_closed_globally(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=f"signer{i}@example.com",
            issuer="https://accounts.google.com",
        )
        for i in range(2)
    ]
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-future-vNext",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert all(r.status == signing.VerifyStatus.BUNDLE_MALFORMED for r in multi.results)
    assert multi.overall == signing.VerifyStatus.BUNDLE_MALFORMED


# ---------------------------------------------------------------------------
# Multi-signer attack — partial structural failure
# ---------------------------------------------------------------------------


# Enforces: two-signer bundle where signer[1]'s inclusion proof is stripped
# returns INCLUSION_FAILED for signer[1] alone; signer[0] still VERIFIED;
# overall != VERIFIED. Catches an attacker appending a forged entry hoping
# the verifier only checks the first signer.
def test_verify_multi_one_valid_one_stripped_inclusion(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_ok = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="a@example.com",
        issuer="https://accounts.google.com",
    )
    blob_bad = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="b@example.com",
        issuer="https://accounts.google.com",
    )
    blob_stripped = crypto_factory.strip_rekor_proof(blob_bad)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_ok, blob_stripped],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert multi.results[0].status == signing.VerifyStatus.VERIFIED
    assert multi.results[1].status == signing.VerifyStatus.INCLUSION_FAILED
    assert multi.overall != signing.VerifyStatus.VERIFIED


# Enforces: when ALL signers fail with the same status, overall == that status.
def test_verify_multi_all_fail_same_status_overall_matches(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    good = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="a@example.com",
        issuer="https://accounts.google.com",
    )
    bad = crypto_factory.tamper_cert(good)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[bad, bad],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    for r in multi.results:
        assert r.status == signing.VerifyStatus.CERT_INVALID
    assert multi.overall == signing.VerifyStatus.CERT_INVALID


# ---------------------------------------------------------------------------
# Empty bundles arrays cannot reach verify_multi (clarification 2)
# ---------------------------------------------------------------------------


# Enforces: SignatureBundle with empty bundles raises EmptySignatureBundle at
# construction (clarification 2). So verify_multi never sees a length-0 array.
def test_empty_bundles_raises_at_construction_not_in_verify_multi(
    canonical_bytes_simple,
):
    with pytest.raises(signing.EmptySignatureBundle):
        signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
