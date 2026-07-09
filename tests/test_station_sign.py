"""Station verify libs — the pure-verify (store-free) subset of the publish
boundary (re-homed from skein_next/tests/test_sign.py, station re-home Stage 2).

These exercise ``skein.sign``'s strict folio verification path and its totality /
single-signer guards directly, with fake signer/verifier branches (no crypto, no
store). The Station/ingress/publish/cli round-trip tests from skein_next's
test_sign.py ride with their server surfaces in Stage 3+ (they are NOT re-homed
here — Stage 2 is libs + tests only).

Failure-injections that MUST FIRE (station re-home §4.2, findings meql/z9mj):
- folio-verify totality: a hostile (non-mapping / bad-field-type / unparseable)
  wire_folio is a TYPED reject, never a raise (test_verify_wire_folio_hostile_*).
- single-signer reject: a multi-blob folio/redeem bundle is rejected BEFORE the
  verifier runs (test_verify_wire_folio_multi_signer_*, test_verify_wire_redeem_multi_signer_*).
The empty-bundle -> BUNDLE_MALFORMED injection lives with the signing primitive it
guards: tests/test_signing/test_verify_multi.py::
test_verify_multi_empty_bundle_mapping_is_bundle_malformed_not_raise.
"""

from __future__ import annotations

import pytest

from skein import signing
from skein import sign as sign_mod
from skein import canon, profile
from skein.identity import compute_folio_hash, hash_token


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


def test_verify_wire_folio_unsigned_is_not_an_error():
    # A wire folio carrying no signature_bundle is 'unsigned' — not an error.
    # skein_next's version seeded a Station and read a folio back through the store;
    # store-free here, since verify_wire_folio takes a plain wire dict — the same
    # short-circuit (raw is None -> 'unsigned') is what's under test.
    wf = {**_FIELDS, "content_hash": compute_folio_hash(_FIELDS)}
    verified, reason, identity = sign_mod.verify_wire_folio(wf, _ok_verifier)
    assert verified is False and reason == "unsigned" and identity is None


# --- strict verification path (ujwx §4) -------------------------------------


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


def test_verify_wire_folio_multi_signer_bundle_rejected_before_crypto():
    # parity with the manifest/redeem single-signer guard (fell finding 1, sibling
    # entrypoint): a v0 folio bundle is single-signer and only results[0] is read, so a
    # multi-blob bundle must be rejected BEFORE the verifier — else a hostile station
    # could pack N valid blobs and force N full Sigstore verifies per `mesh fetch`.
    ch = compute_folio_hash(_FIELDS)
    wf = {**_FIELDS, "content_hash": ch}
    preimage = profile.profiled_preimage(
        profile.CANON_PROFILE_V1, canon.folio_canonical_bytes(wf))
    multi = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["x", "x"],  # two blobs — must be rejected
        canonical_bytes=preimage,
        canon_version=profile.CANON_PROFILE_V1,
    )
    wf["signature_bundle"] = multi.model_dump_json()
    v, seen = _capturing_ok()
    verified, reason, identity = sign_mod.verify_wire_folio(wf, v)
    assert (verified, reason, identity) == (False, "bundle malformed", None)
    assert "bytes" not in seen  # crypto never ran


# --- redeem single-signer guard (the third of manifest/redeem/folio) --------


def _redeem_signer(canonical_bytes):
    """A SignedResult-returning redeem signer (SG2 shape, REDEEM profile)."""
    preimage = profile.profiled_preimage(profile.CANON_PROFILE_REDEEM_V1, canonical_bytes)
    bundle = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["x"],
        canonical_bytes=preimage,
        canon_version=profile.CANON_PROFILE_REDEEM_V1,
    )
    return sign_mod.SignedResult(bundle=bundle, issuer="https://idp", subject="alice")


def test_verify_wire_redeem_multi_signer_bundle_rejected_before_crypto():
    # The third single-signer guard (verify_wire_redeem, sign.py:447). A multi-blob
    # redeem proof must be rejected 'proof malformed' BEFORE the verifier runs — same
    # Sigstore-amplification class as the manifest/folio guards, on the redeem seam.
    token, origin, route = "tok-123", "https://station.example", sign_mod.REDEEM_ROUTE
    proof, _issuer, _subject = sign_mod.sign_redeem_proof(
        token, origin, _redeem_signer, route=route
    )
    bundle = signing.SignatureBundle.model_validate_json(proof["signature_bundle"])
    multi = bundle.model_copy(update={"bundles": [bundle.bundles[0], bundle.bundles[0]]})
    proof["signature_bundle"] = multi.model_dump_json()

    calls = []

    def counting_verifier(canonical_bytes, b):
        calls.append(b)
        return _ok_verifier(canonical_bytes, b)

    verified, reason, identity = sign_mod.verify_wire_redeem(
        proof, hash_token(token), origin, route, counting_verifier
    )
    assert (verified, reason, identity) == (False, "proof malformed", None)
    assert calls == []  # the verifier never ran
