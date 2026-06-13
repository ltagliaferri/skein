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

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from skein import signing

from . import canon, profile
from .identity import content_hash_for_bytes

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


@dataclass(frozen=True)
class SignedResult:
    """A manifest signer's return: the bundle plus the VERIFIED cert identity.

    The Signer return shape carries (issuer, subject) so ``sign_manifest`` can
    surface them — the client records its manifest mirror under its OWN identity
    and the ingress records the manifest with the VERIFIED signer (SG2). The
    pre-change bare-SignatureBundle shape discards the identity and must fail."""

    bundle: "signing.SignatureBundle"
    issuer: str
    subject: str


# A (manifest) Signer maps a descriptor's canonical bytes to a SignedResult.
Signer = Callable[[bytes], SignedResult]
# A Verifier maps (canonical_bytes, bundle) to a MultiVerifyResult.
Verifier = Callable[[bytes, Any], "signing.MultiVerifyResult"]

# Absolute DoS cap on a manifest's declared leaf set, enforced at the verifier
# before any decode / merkle recompute (VM7), so a hostile huge list is never
# processed into unbounded work. Far above any real publish.
_MAX_LEAVES = 1_000_000


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


def build_manifest(constituent_addresses: List[str]) -> Dict[str, Any]:
    """Build the manifest descriptor + leaf list over a set of constituent hashes.

    Returns ``{"root", "leaf_count", "leaf_list"}`` where ``leaf_list`` is the
    sorted, deduped constituent addresses, ``root`` is the framed Merkle root over
    their raw leaf data, and ``leaf_count`` is the distinct-leaf count. The leaf
    set is kind-agnostic — folio hashes and thread hashes are both admissible."""
    data_to_addr: Dict[bytes, str] = {}
    for addr in constituent_addresses:
        data_to_addr[canon.address_to_leaf_datum(addr)] = addr
    leaf_list = [data_to_addr[d] for d in sorted(data_to_addr)]
    root = canon.merkle_root_for_addresses(leaf_list)
    return {"root": root, "leaf_count": len(leaf_list), "leaf_list": leaf_list}


def sign_manifest(
    constituent_addresses: List[str], manifest_signer: Signer
) -> Dict[str, Any]:
    """Sign ONE descriptor over a publish's whole constituent set (SG1).

    Builds the Merkle-root descriptor (folios AND threads as leaves), invokes the
    signer EXACTLY ONCE over the descriptor's canonical bytes — one OIDC ceremony,
    one Fulcio cert, one Rekor entry — and returns the wire ``manifest_signature``::

        {"descriptor": {"root", "leaf_count"},
         "leaf_list":  [sorted deduped constituent hashes],
         "signature_bundle": <bundle json>,
         "issuer": ..., "subject": ...}

    The leaf_list rides as unsigned transport so the verifier can recompute the
    root; the SIGNED bytes are the descriptor only. ``manifest_signer`` must return
    a :class:`SignedResult` (carrying the verified identity); a bare-bundle signer
    fails (SG2)."""
    manifest = build_manifest(constituent_addresses)
    descriptor_bytes = canon.manifest_descriptor_canonical_bytes(
        manifest["root"], manifest["leaf_count"]
    )
    result = manifest_signer(descriptor_bytes)
    return {
        "descriptor": {"root": manifest["root"], "leaf_count": manifest["leaf_count"]},
        "leaf_list": manifest["leaf_list"],
        "signature_bundle": result.bundle.model_dump_json(),
        "issuer": result.issuer,
        "subject": result.subject,
    }


def verify_wire_manifest(
    manifest_signature: Any, verifier: Verifier = None
) -> Tuple[bool, str, Optional[Dict[str, Optional[str]]]]:
    """Verify a wire manifest_signature — TOTAL over a hostile input (Thread C).

    Returns ``(verified, reason, identity)`` where identity is
    ``{"issuer","subject"}`` when verified, else ``None``. NEVER raises, never
    500s. The reason is always one of three DISJOINT BARE kinds — never the
    ingress's 'manifest signature <status>' prefix (the reason-string output
    contract, fell-r1 FIX 1):

    - ABSENCE: 'no manifest' (a well-shaped descriptor-dict carrying no
      signature_bundle, Step 0). The wholly-absent-key case is short-circuited at
      the ingress before this is called.
    - WIRE-INTEGRITY: a bare typed reason 'manifest malformed' / 'unknown profile'
      / 'wrong kind' (Steps 0/1/3).
    - CRYPTO FLOOR: the bare VerifyStatus value (Step 4, status.value).
    """
    if verifier is None:
        verifier = default_verifier

    # Step 0 — SHAPE TOTALITY (guards in order). A present-but-non-dict value
    # (incl. None reaching the verifier) is 'manifest malformed', NOT 'no manifest'.
    if not isinstance(manifest_signature, dict):
        return (False, "manifest malformed", None)
    descriptor = manifest_signature.get("descriptor")
    if (
        not isinstance(descriptor, dict)
        or not isinstance(descriptor.get("root"), str)
        or not isinstance(descriptor.get("leaf_count"), int)
        or isinstance(descriptor.get("leaf_count"), bool)
    ):
        return (False, "manifest malformed", None)
    leaf_list = manifest_signature.get("leaf_list")
    if not isinstance(leaf_list, list) or not all(isinstance(x, str) for x in leaf_list):
        return (False, "manifest malformed", None)
    root = descriptor["root"]
    leaf_count = descriptor["leaf_count"]
    # length-bound BEFORE any decode / merkle recompute (VM7): a hostile huge list
    # is never processed into unbounded work.
    if leaf_count < 0 or leaf_count > _MAX_LEAVES or len(leaf_list) > leaf_count:
        return (False, "manifest malformed", None)
    raw = manifest_signature.get("signature_bundle")
    if raw is None:
        return (False, "no manifest", None)  # ABSENCE bucket (well-shaped, no bundle)
    try:
        bundle = signing.SignatureBundle.model_validate_json(raw)
    except Exception:  # noqa: BLE001 — any parse failure is a malformed manifest
        return (False, "manifest malformed", None)

    # Step 1 — ROOT-vs-LEAFLIST CONSISTENCY (Q6). The recompute proves the SAME
    # tree was rebuilt; leaf_count pins tree SHAPE. A mismatch is 'manifest malformed'.
    try:
        recomputed = canon.merkle_root_for_addresses(leaf_list)
        distinct = canon.dedup_leaf_count(leaf_list)
    except canon.MerkleError:
        return (False, "manifest malformed", None)
    if recomputed != root or distinct != leaf_count:
        return (False, "manifest malformed", None)

    # Step 3 — PROFILE + KIND PIN. Unknown profile and wrong kind are DISTINCT.
    try:
        resolved = profile.get_profile(bundle.canon_version)
    except profile.UnknownProfile:
        return (False, "unknown profile", None)
    if resolved.kind != "manifest":
        return (False, "wrong kind", None)

    # Step 2/4 — INTEGRITY + AUTHORSHIP. verify_multi fails closed if the bundle's
    # stored canonical_bytes diverge from the preimage reconstructed here, so the
    # signed descriptor cannot be edited in flight.
    descriptor_bytes = canon.manifest_descriptor_canonical_bytes(root, leaf_count)
    preimage = profile.profiled_preimage(bundle.canon_version, descriptor_bytes)
    result = verifier(preimage, bundle)
    if result.overall == signing.VerifyStatus.VERIFIED:
        first = result.results[0]
        return (True, "verified", {"issuer": first.issuer, "subject": first.subject})
    return (False, result.overall.value, None)


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

    Scope: this function binds **authorship to the body shown** (step 3 signs the
    body's canonical bytes). It binds the body to the claimed ``content_hash`` when
    one is present (step 1), but it does NOT bind ``content_hash`` to the resolved
    *address* or ``#`` fragment — that is enforced upstream at the resolver/ingress
    (tgg8 §4 step 3) before the envelope is assembled. A "verified" result means
    "these bytes were signed by X", not "this is the folio at that address".
    """
    raw = wire_folio.get("signature_bundle")
    if not raw:
        return (False, "unsigned", None)
    try:
        bundle = signing.SignatureBundle.model_validate_json(raw)
    except Exception:  # noqa: BLE001 — any parse failure is a malformed bundle
        return (False, "bundle malformed", None)

    # (1) integrity: the body must hash to the claimed content hash. Serialize the
    # canonical bytes once and hash those directly (no second serialization).
    canonical = canon.folio_canonical_bytes(wire_folio)
    claimed = wire_folio.get("content_hash")
    if claimed is not None and content_hash_for_bytes(canonical) != claimed:
        return (False, "hash mismatch", None)

    # (2) profile: unknown canon_version is a hard fail, never a downgrade; and a
    # registered profile of the wrong KIND (a manifest bundle presented as a folio)
    # is 'wrong kind', a distinct reason (P7 — the kind pin, legacy verify only).
    try:
        resolved = profile.get_profile(bundle.canon_version)
    except profile.UnknownProfile:
        return (False, "unknown profile", None)
    if resolved.kind != "folio":
        return (False, "wrong kind", None)
    preimage = profile.profiled_preimage(bundle.canon_version, canonical)

    # (3) authorship: verify over the domain-separated preimage. verify_multi
    # binds the signature to THIS folio — it fails closed if the bundle's stored
    # canonical_bytes diverge from the preimage we reconstruct.
    result = verifier(preimage, bundle)
    if result.overall == signing.VerifyStatus.VERIFIED:
        first = result.results[0]
        return (True, "verified", {"issuer": first.issuer, "subject": first.subject})
    return (False, result.overall.value, None)
