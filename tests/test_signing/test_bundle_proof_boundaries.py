"""SignatureBundle and Rekor proof boundary contract.

Oracle finding-20260512-sr0w's bundle/proof nit called out max signer count,
duplicate signer semantics, and inclusion-proof consistency edges.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

MAX_SIGNERS = 256


# Enforces: finding-20260512-sr0w proof-boundary nit. Contract choice: the folio
# wire format allows up to 256 signer bundles. This is high enough for practical
# continuity/delegation use and bounded enough to avoid unbounded verifier work.
def test_signature_bundle_max_signers_documented(canonical_bytes_simple):
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[f'{{"testBundle":{i}}}' for i in range(MAX_SIGNERS)],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    assert len(sb.bundles) == MAX_SIGNERS


# Enforces: the 256-signer cap is hard; callers must not construct bundles that
# imply unbounded verify_multi() work.
def test_signature_bundle_rejects_more_than_max_signers(canonical_bytes_simple):
    with pytest.raises(Exception):
        signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[f'{{"testBundle":{i}}}' for i in range(MAX_SIGNERS + 1)],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )


# Enforces: length-1 is canonical for the common single-signer folio case.
def test_signature_bundle_bundles_length_one_canonical(canonical_bytes_simple):
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=['{"testBundle":0}'],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    assert len(sb.bundles) == 1


phase3_functions = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


# Enforces: duplicate signer identity policy. Contract choice: duplicate
# identities are allowed and verify independently; authorization layers may
# deduplicate them, but verify_multi() is an evidence checker, not a quorum
# calculator.
@phase3_functions
def test_verify_multi_with_duplicate_signer_identity(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    first = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    second = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[first, second],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )

    multi = signing.verify_multi(canonical_bytes_simple, sb)

    assert len(multi.results) == 2
    assert [r.subject for r in multi.results] == ["alice@example.com", "alice@example.com"]
    assert all(r.status == signing.VerifyStatus.VERIFIED for r in multi.results)
    assert multi.overall == signing.VerifyStatus.VERIFIED


# Enforces: a non-trivial Rekor tree needs a non-empty audit path. tree_size=5
# with hashes=[] is a structural bundle defect: the proof cannot even be parsed
# as a valid inclusion proof, so the pinned status is BUNDLE_MALFORMED.
@phase3_functions
def test_rekor_proof_empty_hashes_when_tree_size_greater_than_one(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_rekor_tree_size(blob, 5)
    blob = crypto_factory.set_rekor_hashes(blob, [])
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )

    result = signing.verify(canonical_bytes_simple, sb)

    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: inclusionProof.hashes are base64-encoded Merkle hashes; malformed
# base64 is a bundle-shape error, not a successful empty proof.
@phase3_functions
def test_rekor_proof_malformed_base64_hashes(crypto_factory, canonical_bytes_simple, monkeypatch):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_rekor_hashes(blob, ["not base64!!!"])
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )

    result = signing.verify(canonical_bytes_simple, sb)

    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: checkpoint root and inclusionProof.root_hash bind the same Rekor tree
# root. A valid checkpoint for root X cannot authorize a proof claiming root Y.
@phase3_functions
def test_rekor_proof_checkpoint_root_mismatch_with_proof_root(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_rekor_root_hash(blob, "cm9vdC1oYXNoLW1pc21hdGNo")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )

    result = signing.verify(canonical_bytes_simple, sb)

    assert result.status == signing.VerifyStatus.INCLUSION_FAILED


# ---------------------------------------------------------------------------
# _build_rekor_inclusion_proof — empty key_id contract
# ---------------------------------------------------------------------------
#
# When the wire inclusion proof carries a missing/empty Rekor log_id key_id,
# the builder must honor its "return None means no inclusion proof" contract
# (already used for missing proof at line 689 and for log_index>=tree_size at
# line 704). The earlier `or "unknown"` fallback fabricated a literal "unknown"
# string that satisfies RekorInclusionProof.log_id's min_length=1 validator
# and survived all the way to Evidence consumers, masking the missing-data
# signal.


def _fake_bundle_with_inclusion(
    *,
    key_id: bytes,
    log_index: int = 5,
    tree_size: int = 10,
    integrated_time_s: int = 1_715_443_200,
) -> SimpleNamespace:
    """Build a duck-typed Bundle covering the attributes _build_rekor_inclusion_proof reads."""
    proof = SimpleNamespace(
        log_index=log_index,
        tree_size=tree_size,
        root_hash=b"root-hash-bytes",
        hashes=[b"sibling-0", b"sibling-1"],
        checkpoint=SimpleNamespace(envelope="rekor.sigstore.dev\n10\n<root>\n\n— rekor.sigstore.dev <sig>\n"),
    )
    inner = SimpleNamespace(
        inclusion_proof=proof,
        integrated_time=integrated_time_s,
        log_id=SimpleNamespace(key_id=key_id),
    )
    log_entry = SimpleNamespace(_inner=inner)
    return SimpleNamespace(log_entry=log_entry)


def test_build_rekor_inclusion_proof_returns_none_on_empty_key_id():
    bundle = _fake_bundle_with_inclusion(key_id=b"")
    assert signing._build_rekor_inclusion_proof(bundle) is None


def test_build_rekor_inclusion_proof_b64_encodes_log_id_when_key_id_present():
    key_id = b"\x01\x02\x03\x04rekor-key"
    bundle = _fake_bundle_with_inclusion(key_id=key_id)
    result = signing._build_rekor_inclusion_proof(bundle)
    assert result is not None
    assert result.log_id == base64.b64encode(key_id).decode("ascii")
