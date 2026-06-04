"""Signing at the publish boundary — the seam between the publish path and Sigstore.

This wires the dormant ``skein.signing`` primitive (sign / verify_multi) into the
client->instance publish path, per brief-20260522-q8k0 and the publish spec
(brief-20260601-nqtj). The design is sign-at-publish, not sign-at-post: local
posts stay crypto-free; the signing happens once, at the boundary.

Two seams, both pluggable so the wiring is testable without a live Sigstore:

- ``Signer`` (client): turns a folio's canonical bytes into a ``SignatureBundle``.
  The real signer wraps ``skein.signing.sign`` against an OIDC session — which
  needs an interactive login, the human-accountability gate. Tests inject a fake.
- verification (instance + read): ``verify_wire_folio`` re-derives the folio's
  canonical bytes and hands them to ``verify_multi``, which fails closed if the
  bundle's own ``canonical_bytes`` disagree (binding the signature to THIS folio)
  and otherwise reports the Sigstore verdict.

The bytes signed are exactly the bytes hashed — both come from
``canon.folio_canonical_bytes`` — so a folio's identity and its signature can
never disagree on what was covered.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from skein import signing

from . import canon, profile
from .identity import compute_folio_hash

DEFAULT_IDENTITY_SCHEME = "sigstore-public-v1"

# Sigstore's public production Dex broker. The interactive flow federates to
# Google/GitHub/Microsoft and mints a token whose issuer is the broker — which is
# on the v0 allowlist (skein.signing._V0_OIDC_ALLOWLIST), so sign() accepts it.
# Production only for v0: sign()/verify deliberately use the production trust root
# (no staging path), so signing a staging-issued token would pass the local
# guards and then fail confusingly at production Fulcio. Don't offer staging here
# until a staging context is plumbed through sign() and verify together.
SIGSTORE_PROD_ISSUER = "https://oauth2.sigstore.dev/auth"


def acquire_oidc_provider(
    issuer_url: Optional[str] = None,
    force_oob: bool = False,
) -> "signing.OIDCProviderConfig":
    """Run the interactive Sigstore OIDC flow and return a ready provider config.

    This is the human-accountability gate: it opens a browser (or, with
    ``force_oob``, prints a URL to paste a code back — for SSH/headless) and
    returns a short-lived token bound to the operator's identity. The token's own
    issuer claim is used, so the v0 allowlist check in ``sign()`` passes. Network
    + interactive by nature — exercised at the ceremony, not in CI.
    """
    from sigstore.oidc import Issuer  # lazy: only the ceremony needs the browser flow

    identity_token = Issuer(issuer_url or SIGSTORE_PROD_ISSUER).identity_token(force_oob=force_oob)
    return signing.OIDCProviderConfig(
        issuer=identity_token.issuer,
        token=str(identity_token),
        provider_id=None,
    )


# A Signer maps a folio's canonical bytes to a SignatureBundle.
Signer = Callable[[bytes], "signing.SignatureBundle"]
# A Verifier maps (canonical_bytes, bundle) to a MultiVerifyResult.
Verifier = Callable[[bytes, Any], "signing.MultiVerifyResult"]


def make_oidc_signer(
    oidc_provider: "signing.OIDCProviderConfig",
    identity_scheme: str = DEFAULT_IDENTITY_SCHEME,
    trust_root_pin: Optional[str] = None,
    canon_profile: str = profile.CANON_PROFILE_V1,
) -> Signer:
    """A Signer that signs each folio under ``oidc_provider`` via public Sigstore.

    Producing the ``oidc_provider`` token is the interactive ceremony (a Google
    login) — the caller owns it. One provider/session signs the whole publish
    batch (the in-memory Fulcio cert is reused across the calls).

    Domain separation (§3-4): what gets signed is not the raw folio canonical
    bytes but ``profiled_preimage(profile, canonical_bytes)`` — and the bundle
    records that preimage as its ``canonical_bytes`` and the profile as its
    ``canon_version``, so the verifier can reconstruct and check the exact same
    domain-separated bytes.
    """

    def _sign(canonical_bytes: bytes) -> "signing.SignatureBundle":
        preimage = profile.profiled_preimage(canon_profile, canonical_bytes)
        result = signing.sign(preimage, oidc_provider)  # SignResult
        return signing.SignatureBundle(
            identity_scheme=identity_scheme,
            bundles=[result.bundle_json],
            canonical_bytes=preimage,
            canon_version=canon_profile,
            trust_root_pin=trust_root_pin,
        )

    return _sign


def sign_wire_folio(wire_folio: Mapping[str, Any], signer: Signer) -> Dict[str, Any]:
    """Return a copy of ``wire_folio`` with a ``signature_bundle`` attached.

    The bundle is JSON (canonical_bytes base64-encoded by the model). It is
    overlay — ``signature_bundle`` is not one of the five canonical fields, so it
    never enters the content hash.
    """
    bundle = signer(canon.folio_canonical_bytes(wire_folio))
    signed = dict(wire_folio)
    signed["signature_bundle"] = bundle.model_dump_json()
    return signed


def default_verifier(canonical_bytes: bytes, bundle: Any) -> "signing.MultiVerifyResult":
    # We deliberately do not pin an expected identity — this is a read surface
    # that *displays* whoever validly signed, not a gate against one signer. So
    # sigstore verifies the signature, the Fulcio chain, and Rekor inclusion, but
    # its identity policy is UnsafeNoOp, which logs a scary "no verification
    # performed!" warning every time. That warning is expected here (the crypto
    # IS checked; only identity-pinning is skipped), so silence just that one
    # logger for the duration of our call — a real warning from anywhere else
    # still surfaces.
    import logging

    policy_logger = logging.getLogger("sigstore.verify.policy")
    previous = policy_logger.level
    policy_logger.setLevel(logging.ERROR)
    try:
        return signing.verify_multi(canonical_bytes, bundle)
    finally:
        policy_logger.setLevel(previous)


def verify_wire_folio(
    wire_folio: Mapping[str, Any], verifier: Verifier = default_verifier
) -> Tuple[bool, str, Optional[Dict[str, Optional[str]]]]:
    """The strict verification path (brief-20260603-ujwx §4) — the only path.

    Returns ``(verified, reason, identity)`` where identity is
    ``{"issuer", "subject"}`` when verified, else ``None``. An unsigned folio
    returns ``(False, "unsigned", None)`` — not an error, just no signature.

    The three strict steps, in order:

    1. **Integrity.** Re-serialize the folio body through canon and hash it; if a
       ``content_hash`` is claimed, it MUST equal the recomputed hash, or it's a
       ``hash mismatch`` before any crypto is touched (a tampered body can't ride
       a valid bundle).
    2. **Profile.** The bundle's ``canon_version`` must resolve in the profile
       registry; an **unknown profile is a hard failure**, never a fallback (§3).
    3. **Authorship.** Verify the signature over the domain-separated preimage
       ``profile || recomputed-canonical-bytes`` (``verify_multi`` also fails
       closed if the bundle's stored bytes diverge from these), yielding the
       signer identity or a typed failure.

    There is no lazy path: nothing is accepted on the bundle's own stored bytes
    without re-deriving them from the body shown.
    """
    raw = wire_folio.get("signature_bundle")
    if not raw:
        return (False, "unsigned", None)
    try:
        bundle = signing.SignatureBundle.model_validate_json(raw)
    except Exception:  # noqa: BLE001 — any parse failure is a malformed bundle
        return (False, "bundle malformed", None)

    # (1) integrity: the body must hash to the claimed content hash.
    canonical = canon.folio_canonical_bytes(wire_folio)
    claimed = wire_folio.get("content_hash")
    if claimed is not None and compute_folio_hash(wire_folio) != claimed:
        return (False, "hash mismatch", None)

    # (2) profile: unknown canon_version is a hard fail, never a downgrade.
    try:
        preimage = profile.profiled_preimage(bundle.canon_version, canonical)
    except profile.UnknownProfile:
        return (False, "unknown profile", None)

    # (3) authorship: verify over the domain-separated preimage. verify_multi
    # binds the signature to THIS folio — it fails closed if the bundle's stored
    # canonical_bytes diverge from the preimage we reconstruct.
    result = verifier(preimage, bundle)
    if result.overall == signing.VerifyStatus.VERIFIED:
        first = result.results[0]
        return (True, "verified", {"issuer": first.issuer, "subject": first.subject})
    return (False, result.overall.value, None)
