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

from . import canon

DEFAULT_IDENTITY_SCHEME = "sigstore-public-v1"

# A Signer maps a folio's canonical bytes to a SignatureBundle.
Signer = Callable[[bytes], "signing.SignatureBundle"]
# A Verifier maps (canonical_bytes, bundle) to a MultiVerifyResult.
Verifier = Callable[[bytes, Any], "signing.MultiVerifyResult"]


def make_oidc_signer(
    oidc_provider: "signing.OIDCProviderConfig",
    identity_scheme: str = DEFAULT_IDENTITY_SCHEME,
    trust_root_pin: Optional[str] = None,
) -> Signer:
    """A Signer that signs each folio under ``oidc_provider`` via public Sigstore.

    Producing the ``oidc_provider`` token is the interactive ceremony (a Google
    login) — the caller owns it. One provider/session signs the whole publish
    batch (the in-memory Fulcio cert is reused across the calls).
    """
    def _sign(canonical_bytes: bytes) -> "signing.SignatureBundle":
        result = signing.sign(canonical_bytes, oidc_provider)  # SignResult
        return signing.SignatureBundle(
            identity_scheme=identity_scheme,
            bundles=[result.bundle_json],
            canonical_bytes=canonical_bytes,
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
    return signing.verify_multi(canonical_bytes, bundle)


def verify_wire_folio(
    wire_folio: Mapping[str, Any], verifier: Verifier = default_verifier
) -> Tuple[bool, str, Optional[Dict[str, Optional[str]]]]:
    """Verify a wire folio's signature against its own canonical bytes.

    Returns ``(verified, reason, identity)`` where identity is
    ``{"issuer", "subject"}`` when verified, else ``None``. An unsigned folio
    returns ``(False, "unsigned", None)`` — not an error, just no signature.
    """
    raw = wire_folio.get("signature_bundle")
    if not raw:
        return (False, "unsigned", None)
    try:
        bundle = signing.SignatureBundle.model_validate_json(raw)
    except Exception:  # noqa: BLE001 — any parse failure is a malformed bundle
        return (False, "bundle malformed", None)

    # verify_multi binds the signature to THIS folio: it fails closed if the
    # bundle's stored canonical_bytes diverge from the bytes we pass.
    expected = canon.folio_canonical_bytes(wire_folio)
    result = verifier(expected, bundle)
    if result.overall == signing.VerifyStatus.VERIFIED:
        first = result.results[0]
        return (True, "verified", {"issuer": first.issuer, "subject": first.subject})
    return (False, result.overall.value, None)
