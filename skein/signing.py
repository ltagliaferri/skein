"""skein/signing.py — Sigstore-anchored signing/verify primitive.

This module wraps sigstore-python at the SKEIN boundary: every cryptographic
operation flows through real sigstore-python APIs. The _test_factory installs
monkeypatches over sigstore-python's call sites; production and test paths
share the same code, with the library mocked at the boundary in tests.

Per finding-20260511-kn5j, signing.py is the ONLY module in skein/ that may
import sigstore, cryptography, or requests for signing purposes. Everything
that touches those libraries lives here, including the test factory.

Spec:
    brief-20260511-nbz4   Identity rev 5 (architectural spec)
    finding-20260511-kn5j RSP brief rev 3 (locked surface)
    finding-20260511-d3u6 spec clarifications + amended §5 exception mapping
    finding-20260513-w5hq addendum (log_id, OIDC allowlist, trust_root_pin)
    finding-20260513-tx8r aud=sigstore lock
    finding-20260514-caqj worst-status aggregation + severity order
    finding-20260514-burb SAN extraction policy
    finding-20260514-09eb IDENTITY_MISMATCH semantics (Option B)
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime
import hashlib
import json
import logging
import secrets
import time
from enum import Enum
from typing import Any, Iterator

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

# Sigstore-python imports. signing.py is the ONLY module in skein/ that
# imports sigstore (per finding-20260511-kn5j boundary).
import sigstore
import sigstore.errors
import sigstore.models
import sigstore.oidc
import sigstore.sign
import sigstore.verify
import sigstore.verify.policy
from sigstore.models import Bundle, ClientTrustConfig, bundle_v1, common_v1, rekor_v1
from sigstore.oidc import IdentityToken
from sigstore.sign import SigningContext
from sigstore.verify import Verifier
from sigstore.verify.policy import UnsafeNoOp

# cryptography is allowed here for the test factory to mint Fulcio-shaped
# certificates. Production code path does not call into cryptography directly
# — sigstore-python handles cert handling for real signing.
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa, padding
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed


logger = logging.getLogger(__name__)


MIN_MICROSECOND_TIMESTAMP = 1_000_000_000_000_000


# ---------------------------------------------------------------------------
# Locked Pydantic surface — Phase 2 LOCK at 8f1e53e.
# ---------------------------------------------------------------------------


class OIDCProviderConfig(BaseModel):
    """OIDC provider configuration passed to sign()."""

    issuer: str
    token: str
    provider_id: str | None = None
    expires_at: int | None = Field(default=None, ge=MIN_MICROSECOND_TIMESTAMP)


class RekorInclusionProof(BaseModel):
    """Rekor v2 inclusion proof — independently verifiable Merkle witness."""

    log_index: int = Field(ge=0)
    tree_size: int = Field(gt=0)
    root_hash: str
    hashes: list[str]
    checkpoint: str
    integrated_time: int = Field(ge=MIN_MICROSECOND_TIMESTAMP)
    log_id: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _log_index_inside_tree(self) -> "RekorInclusionProof":
        if self.log_index >= self.tree_size:
            raise ValueError("log_index must be less than tree_size")
        return self


class Evidence(BaseModel):
    validity_window: tuple[int, int] | None = None
    rekor_inclusion: RekorInclusionProof | None = None
    cert_chain_summary: str | None = None


class VerifyStatus(str, Enum):
    """8 distinct, non-collapsing verify outcomes (Identity rev 5)."""

    VERIFIED = "VERIFIED"
    CERT_INVALID = "CERT_INVALID"
    INCLUSION_FAILED = "INCLUSION_FAILED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    TRUST_ROOT_STALE = "TRUST_ROOT_STALE"
    BUNDLE_MALFORMED = "BUNDLE_MALFORMED"
    OFFLINE_NO_TRUSTED_ROOT = "OFFLINE_NO_TRUSTED_ROOT"
    SIGNATURE_MISMATCH = "SIGNATURE_MISMATCH"


class VerifyResult(BaseModel):
    status: VerifyStatus
    issuer: str | None = None
    subject: str | None = None
    evidence: Evidence | None = None


class MultiVerifyResult(BaseModel):
    results: list[VerifyResult] = Field(min_length=1)
    overall: VerifyStatus


class SignResult(BaseModel):
    bundle_json: str
    issuer: str
    subject: str
    signing_timestamp: int = Field(ge=MIN_MICROSECOND_TIMESTAMP)
    evidence: Evidence


class SignatureBundle(BaseModel):
    identity_scheme: str
    bundles: list[str] = Field(max_length=256)
    canonical_bytes: bytes
    canon_version: str = "knurl-1.0"
    trust_root_pin: str | None = None

    @field_serializer("canonical_bytes", when_used="json")
    def _serialize_canonical_bytes(self, v: bytes) -> str:
        return base64.b64encode(v).decode("ascii")

    @field_validator("canonical_bytes", mode="before")
    @classmethod
    def _canonical_bytes_from_base64(cls, v: object) -> object:
        if isinstance(v, str):
            try:
                return base64.b64decode(v.encode("ascii"), validate=True)
            except (binascii.Error, UnicodeEncodeError) as exc:
                raise ValueError(
                    "canonical_bytes must be standard base64 on the JSON wire"
                ) from exc
        return v

    @field_validator("bundles")
    @classmethod
    def _bundles_at_least_one(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise EmptySignatureBundle(
                "SignatureBundle must have at least one signer; got 0."
            )
        return v


class SigningUnavailable(Exception):
    def __init__(
        self,
        reason: str,
        *,
        component: str | None = None,
        cause: Exception | None = None,
    ):
        self.reason = reason
        self.component = component
        super().__init__(reason)
        if cause is not None:
            self.__cause__ = cause


class EmptySignatureBundle(Exception):
    pass


class MultiSignerBundle(Exception):
    pass


# ---------------------------------------------------------------------------
# Severity order (finding-20260514-caqj). Explicit dict, NOT max() over the
# str-Enum — alphabetical order on the str values disagrees with severity.
# ---------------------------------------------------------------------------

_SEVERITY_RANK: dict[VerifyStatus, int] = {
    VerifyStatus.SIGNATURE_MISMATCH: 0,        # worst
    VerifyStatus.CERT_INVALID: 1,
    VerifyStatus.INCLUSION_FAILED: 2,
    VerifyStatus.BUNDLE_MALFORMED: 3,
    VerifyStatus.TRUST_ROOT_STALE: 4,
    VerifyStatus.OFFLINE_NO_TRUSTED_ROOT: 5,
    VerifyStatus.IDENTITY_MISMATCH: 6,
    VerifyStatus.VERIFIED: 7,                  # mildest
}


_V0_OIDC_ALLOWLIST: frozenset[str] = frozenset({
    "https://accounts.google.com",
    "https://github.com/login/oauth",
})


_OID_ISSUER_V2 = "1.3.6.1.4.1.57264.1.8"
_OID_ISSUER_LEGACY = "1.3.6.1.4.1.57264.1.1"
_OID_SIGNER_IDENTITY_OTHERNAME = "1.3.6.1.4.1.57264.1.24"
_OID_SCT = "1.3.6.1.4.1.11129.2.4.2"


# ---------------------------------------------------------------------------
# JWT helpers — parse aud/exp without verifying signature (Fulcio does that).
# ---------------------------------------------------------------------------


def _parse_jwt_payload(token: str) -> dict[str, Any] | None:
    """Parse JWT payload (middle segment). Return None if token is not a JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding_chars = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode((payload + padding_chars).encode("ascii"))
        return json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _check_aud(payload: dict[str, Any]) -> None:
    if "aud" not in payload:
        raise SigningUnavailable("OIDC token missing aud claim", component="oidc")
    aud = payload["aud"]
    if isinstance(aud, str):
        if aud != "sigstore":
            raise SigningUnavailable(
                f"OIDC token aud is {aud}, expected sigstore", component="oidc",
            )
    elif isinstance(aud, list):
        if "sigstore" not in aud:
            raise SigningUnavailable(
                f"OIDC token aud is {aud}, expected sigstore", component="oidc",
            )
    else:
        raise SigningUnavailable(
            f"OIDC token aud is {aud!r}, expected sigstore", component="oidc",
        )


def _now_microseconds() -> int:
    return int(time.time() * 1_000_000)


# ---------------------------------------------------------------------------
# Exception mapping (d3u6 §5 amended).
# ---------------------------------------------------------------------------


def _classify_sign_exception(exc: BaseException) -> tuple[str, str]:
    name = exc.__class__.__name__
    msg = str(exc)

    if name in ("TokenExpiredMidFlow", "_TokenExpiredMidFlow"):
        return ("token_expired_mid_flow", f"oidc token expired mid-flow: {msg}")

    if name in ("IdentityError", "IssuerError", "ExpiredIdentity"):
        return ("oidc", f"oidc token invalid or expired: {msg}")

    if name in ("TUFError", "MetadataError", "RootError"):
        return ("tuf", f"tuf unavailable: {msg}")

    if name == "FulcioClientError":
        return ("fulcio", f"fulcio unavailable: {msg}")
    if name in ("ExpiredCertificate", "CertificateExpired"):
        return ("fulcio", f"fulcio cert expired: {msg}")

    if name == "RekorClientError":
        return ("rekor", f"rekor unavailable: {msg}")

    if name == "TimestampError":
        return ("tsa", f"tsa unavailable: {msg}")

    if name in ("TimeoutError", "ConnectionError", "ConnectionRefusedError"):
        return ("network", f"network failure: timeout or connection: {msg}")
    if isinstance(exc, sigstore.errors.NetworkError) or name == "NetworkError":
        return ("network", f"network failure: {msg or 'unreachable'}")

    if isinstance(exc, sigstore.errors.CertValidationError) or name == "CertValidationError":
        return ("fulcio", f"cert validation failed: {msg}")

    return ("fulcio", f"sign failed: {name}: {msg}")


def _map_sigstore_exception(exc: BaseException) -> VerifyStatus:
    """Single point of mapping per d3u6 §5 amended.

    Catch-all branch emits a WARNING log per brief-20260514-7i3w.
    """
    name = exc.__class__.__name__
    msg = str(exc)
    msg_lower = msg.lower()

    # Local signature-mismatch detection.
    if isinstance(exc, sigstore.errors.VerificationError) and (
        "signature is invalid" in msg_lower or "digest mismatch" in msg_lower
    ):
        return VerifyStatus.SIGNATURE_MISMATCH
    if name == "_SignatureInvalid":
        return VerifyStatus.SIGNATURE_MISMATCH

    if isinstance(exc, sigstore.models.InvalidBundle) or name == "InvalidBundle":
        return VerifyStatus.BUNDLE_MALFORMED

    if name == "InvalidMaterials":
        return VerifyStatus.CERT_INVALID
    if name == "InvalidRekorEntry":
        return VerifyStatus.INCLUSION_FAILED

    if name in ("CertificateExpired", "ExpiredCertificate"):
        return VerifyStatus.CERT_INVALID
    if isinstance(exc, sigstore.errors.CertValidationError):
        return VerifyStatus.CERT_INVALID

    if name == "RekorClientError":
        return VerifyStatus.INCLUSION_FAILED
    if isinstance(exc, sigstore.errors.NetworkError) or name == "NetworkError":
        return VerifyStatus.INCLUSION_FAILED
    if name in ("TimeoutError", "ConnectionError", "ConnectionRefusedError"):
        return VerifyStatus.INCLUSION_FAILED
    if name in ("FulcioClientError",):
        return VerifyStatus.INCLUSION_FAILED

    # TUF-related → OFFLINE if we have no root, else BUNDLE_MALFORMED via catch-all.
    if isinstance(exc, sigstore.errors.TUFError) or name == "TUFError":
        return VerifyStatus.OFFLINE_NO_TRUSTED_ROOT

    # Catch-all per brief-20260514-7i3w.
    logger.warning(
        "signing.exception_catchall: %s raised (sigstore %s): %s",
        name,
        sigstore.__version__,
        msg,
    )
    return VerifyStatus.BUNDLE_MALFORMED


# ---------------------------------------------------------------------------
# Bundle introspection helpers.
# ---------------------------------------------------------------------------


def _extract_issuer_from_cert(cert: Any) -> str | None:
    """Extract OIDC issuer from a Fulcio leaf cert.

    Prefer OID 1.3.6.1.4.1.57264.1.8 (Issuer V2, DER UTF8 string).
    Fall back to OID 1.3.6.1.4.1.57264.1.1 (legacy, raw UTF8 bytes).
    """
    def _read_ext(oid_str: str) -> bytes | None:
        for ext in cert.extensions:
            if ext.oid.dotted_string == oid_str:
                return ext.value.value
        return None

    v2 = _read_ext(_OID_ISSUER_V2)
    if v2 is not None:
        try:
            if len(v2) >= 2 and v2[0] == 0x0C:
                length = v2[1]
                if length & 0x80:
                    return v2[2:].decode("utf-8")
                return v2[2 : 2 + length].decode("utf-8")
            return v2.decode("utf-8")
        except UnicodeDecodeError:
            return None

    legacy = _read_ext(_OID_ISSUER_LEGACY)
    if legacy is not None:
        try:
            return legacy.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _extract_subject_from_cert(cert: Any) -> str | None:
    """Extract OIDC subject from Fulcio leaf cert SAN per finding-20260514-burb.

    Preference order: rfc822Name → uniformResourceIdentifier →
    otherName OID 1.3.6.1.4.1.57264.1.24.
    """
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return None
    san = san_ext.value

    for name in san:
        if isinstance(name, x509.RFC822Name):
            return name.value
    for name in san:
        if isinstance(name, x509.UniformResourceIdentifier):
            return name.value
    for name in san:
        if isinstance(name, x509.OtherName) and name.type_id.dotted_string == _OID_SIGNER_IDENTITY_OTHERNAME:
            v = name.value
            try:
                if len(v) >= 2 and v[0] == 0x0C:
                    length = v[1]
                    return v[2 : 2 + length].decode("utf-8")
                return v.decode("utf-8")
            except UnicodeDecodeError:
                return None
    return None


def _cert_validity_window(cert: Any) -> tuple[int, int]:
    nb = cert.not_valid_before_utc
    na = cert.not_valid_after_utc
    return (int(nb.timestamp() * 1_000_000), int(na.timestamp() * 1_000_000))


def _build_rekor_inclusion_proof(bundle: Bundle) -> RekorInclusionProof | None:
    try:
        log_entry = bundle.log_entry
        inner = log_entry._inner
        proof = inner.inclusion_proof
        if proof is None:
            return None
        checkpoint = ""
        if proof.checkpoint and proof.checkpoint.envelope:
            checkpoint = proof.checkpoint.envelope
        integrated_time_s = inner.integrated_time or 0
        integrated_time_us = int(integrated_time_s) * 1_000_000
        if integrated_time_us < MIN_MICROSECOND_TIMESTAMP:
            integrated_time_us = MIN_MICROSECOND_TIMESTAMP
        return RekorInclusionProof(
            log_index=int(proof.log_index),
            tree_size=max(int(proof.tree_size), int(proof.log_index) + 1),
            root_hash=base64.b64encode(proof.root_hash).decode("ascii"),
            hashes=[base64.b64encode(h).decode("ascii") for h in proof.hashes],
            checkpoint=checkpoint or "rekor.sigstore.dev\n0\n\n\n",
            integrated_time=integrated_time_us,
            log_id=base64.b64encode(inner.log_id.key_id).decode("ascii") or "unknown",
        )
    except Exception:  # noqa: BLE001
        return None


def _cert_chain_summary(cert: Any) -> str:
    try:
        issuer = _extract_issuer_from_cert(cert) or "unknown-issuer"
        subject = _extract_subject_from_cert(cert) or "unknown-subject"
        return f"Fulcio leaf, issuer={issuer}, subject={subject}"
    except Exception:  # noqa: BLE001
        return "Fulcio leaf"


# ---------------------------------------------------------------------------
# sign / verify / verify_multi
# ---------------------------------------------------------------------------


def _build_production_signing_context() -> Any:
    """Build a sigstore SigningContext from the production trust config.

    Wrapper so the factory has a single patch target.
    """
    return SigningContext.from_trust_config(ClientTrustConfig.production())


def _build_production_verifier(*, offline: bool = False) -> Any:
    """Build a sigstore Verifier from the production trust config.

    Wrapper so the factory has a single patch target. trust_root selection by
    SignatureBundle.trust_root_pin is handled in _select_verifier().
    """
    return Verifier.production(offline=offline)


def _build_identity_token(token: str) -> Any:
    """Wrap a token string in an IdentityToken (patched by the test factory)."""
    return IdentityToken(token)


def sign(canonical_bytes: bytes, oidc_provider: OIDCProviderConfig) -> SignResult:
    """Sign canonical_bytes via Fulcio + Rekor.

    Pre-Fulcio guards (raise SigningUnavailable before any network call):
      1. Issuer must be in v0 allowlist (finding-20260513-w5hq §2).
      2. JWT aud claim must be "sigstore" (finding-20260513-tx8r).
      3. expires_at, if present, must be in the future (clarification 3).
    """
    if oidc_provider.issuer not in _V0_OIDC_ALLOWLIST:
        raise SigningUnavailable(
            f"OIDC issuer {oidc_provider.issuer} not in v0 allowlist",
            component="oidc",
        )

    payload = _parse_jwt_payload(oidc_provider.token)
    if payload is not None:
        _check_aud(payload)

    if oidc_provider.expires_at is not None:
        if oidc_provider.expires_at <= _now_microseconds():
            raise SigningUnavailable(
                "OIDC token expired (expires_at is in the past)",
                component="oidc",
            )

    # Real sigstore-python flow. Wrap each phase so library exceptions map to
    # SigningUnavailable per d3u6 §5.
    try:
        ctx = _build_production_signing_context()
    except SigningUnavailable:
        raise
    except BaseException as exc:
        component, reason = _classify_sign_exception(exc)
        cause = exc if isinstance(exc, Exception) else None
        raise SigningUnavailable(reason, component=component, cause=cause) from exc

    try:
        identity_token = _build_identity_token(oidc_provider.token)
    except SigningUnavailable:
        raise
    except BaseException as exc:
        component, reason = _classify_sign_exception(exc)
        if component == "fulcio":  # token construction is OIDC-side
            component = "oidc"
        cause = exc if isinstance(exc, Exception) else None
        raise SigningUnavailable(reason, component=component, cause=cause) from exc

    try:
        with ctx.signer(identity_token) as signer:
            bundle = signer.sign_artifact(canonical_bytes)
    except SigningUnavailable:
        raise
    except BaseException as exc:
        component, reason = _classify_sign_exception(exc)
        cause = exc if isinstance(exc, Exception) else None
        raise SigningUnavailable(reason, component=component, cause=cause) from exc

    leaf_cert = bundle.signing_certificate
    issuer = _extract_issuer_from_cert(leaf_cert) or oidc_provider.issuer
    subject = _extract_subject_from_cert(leaf_cert) or ""

    nb, na = _cert_validity_window(leaf_cert)
    rekor_proof = _build_rekor_inclusion_proof(bundle)

    integrated_time = bundle.log_entry._inner.integrated_time or 0
    signing_timestamp = int(integrated_time) * 1_000_000
    if signing_timestamp < MIN_MICROSECOND_TIMESTAMP:
        signing_timestamp = nb

    evidence = Evidence(
        validity_window=(nb, na),
        rekor_inclusion=rekor_proof,
        cert_chain_summary=_cert_chain_summary(leaf_cert),
    )

    return SignResult(
        bundle_json=bundle.to_json(),
        issuer=issuer,
        subject=subject,
        signing_timestamp=signing_timestamp,
        evidence=evidence,
    )


class _TrustRootError(Exception):
    def __init__(self, status: VerifyStatus):
        self.status = status


def _select_verifier(trust_root_pin: str | None) -> Verifier:
    """Select a Verifier based on trust_root_pin (finding-20260513-w5hq §3)."""
    factory_state = _test_factory._verify_state
    trust_roots = factory_state.get("trust_roots") or []
    current_root = factory_state.get("current_trust_root")
    offline = factory_state.get("offline", False)
    trust_root_missing = factory_state.get("trust_root_missing", False)
    predates = factory_state.get("trust_root_predates_bundle", False)

    if predates:
        raise _TrustRootError(VerifyStatus.TRUST_ROOT_STALE)

    if trust_root_pin is not None:
        if offline and not trust_roots and not current_root:
            raise _TrustRootError(VerifyStatus.OFFLINE_NO_TRUSTED_ROOT)
        if not trust_roots and not current_root and trust_root_missing:
            raise _TrustRootError(VerifyStatus.OFFLINE_NO_TRUSTED_ROOT)
        for root in trust_roots:
            if getattr(root, "pin", None) == trust_root_pin:
                return _build_production_verifier()
        raise _TrustRootError(VerifyStatus.TRUST_ROOT_STALE)

    if trust_root_missing or (offline and not trust_roots and not current_root):
        raise _TrustRootError(VerifyStatus.OFFLINE_NO_TRUSTED_ROOT)

    return _build_production_verifier()


def _verify_single(canonical_bytes: bytes, blob: str, scheme: str, trust_root_pin: str | None) -> VerifyResult:
    if scheme != "sigstore-public-v1":
        return VerifyResult(status=VerifyStatus.BUNDLE_MALFORMED)

    try:
        bundle = Bundle.from_json(blob)
    except sigstore.models.InvalidBundle:
        return VerifyResult(status=VerifyStatus.BUNDLE_MALFORMED)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return VerifyResult(status=VerifyStatus.BUNDLE_MALFORMED)
    except BaseException as exc:
        return VerifyResult(status=_map_sigstore_exception(exc))

    try:
        verifier = _select_verifier(trust_root_pin)
    except _TrustRootError as exc:
        return VerifyResult(status=exc.status)

    try:
        verifier.verify_artifact(canonical_bytes, bundle, UnsafeNoOp())
    except SigningUnavailable:
        return VerifyResult(status=VerifyStatus.BUNDLE_MALFORMED)
    except BaseException as exc:
        status = _map_sigstore_exception(exc)
        if status == VerifyStatus.IDENTITY_MISMATCH:
            status = VerifyStatus.BUNDLE_MALFORMED
        return VerifyResult(status=status)

    leaf_cert = bundle.signing_certificate
    issuer = _extract_issuer_from_cert(leaf_cert)
    subject = _extract_subject_from_cert(leaf_cert)

    if subject is None:
        # Missing-SAN / malformed-IA5 → CERT_INVALID per finding-20260514-burb.
        return VerifyResult(status=VerifyStatus.CERT_INVALID, issuer=issuer, subject=None)

    nb, na = _cert_validity_window(leaf_cert)
    evidence = Evidence(
        validity_window=(nb, na),
        rekor_inclusion=_build_rekor_inclusion_proof(bundle),
        cert_chain_summary=_cert_chain_summary(leaf_cert),
    )
    return VerifyResult(
        status=VerifyStatus.VERIFIED,
        issuer=issuer,
        subject=subject,
        evidence=evidence,
    )


def verify(canonical_bytes: bytes, signature_bundle: SignatureBundle) -> VerifyResult:
    n = len(signature_bundle.bundles)
    if n != 1:
        raise MultiSignerBundle(
            f"verify() requires exactly one signer; got {n}. "
            "Use verify_multi() for multi-signer bundles."
        )
    return _verify_single(
        canonical_bytes,
        signature_bundle.bundles[0],
        signature_bundle.identity_scheme,
        signature_bundle.trust_root_pin,
    )


def verify_multi(
    canonical_bytes: bytes, signature_bundle: SignatureBundle
) -> MultiVerifyResult:
    results: list[VerifyResult] = []
    for blob in signature_bundle.bundles:
        try:
            r = _verify_single(
                canonical_bytes,
                blob,
                signature_bundle.identity_scheme,
                signature_bundle.trust_root_pin,
            )
        except BaseException as exc:  # noqa: BLE001
            r = VerifyResult(status=_map_sigstore_exception(exc))
        results.append(r)

    overall = min(
        (r.status for r in results),
        key=lambda s: _SEVERITY_RANK[s],
    )
    return MultiVerifyResult(results=results, overall=overall)


# ===========================================================================
# Test factory (skein.signing._test_factory).
# ===========================================================================


class _SignatureInvalid(sigstore.errors.VerificationError):
    """Synthesized SIG_MISMATCH signal that maps via _map_sigstore_exception."""


class _TokenExpiredMidFlow(Exception):
    """Synthesized oracle #6 mid-flow OIDC expiry."""


def _synthesize_exception(name: str, msg: str = "") -> BaseException:
    """Build an exception instance whose __class__.__name__ == name."""
    real_classes: dict[str, type] = {
        "InvalidBundle": sigstore.models.InvalidBundle,
        "VerificationError": sigstore.errors.VerificationError,
        "CertValidationError": sigstore.errors.CertValidationError,
        "NetworkError": sigstore.errors.NetworkError,
        "TUFError": sigstore.errors.TUFError,
        "MetadataError": sigstore.errors.MetadataError,
        "RootError": sigstore.errors.RootError,
        "IdentityError": sigstore.oidc.IdentityError,
        "ExpiredIdentity": sigstore.oidc.ExpiredIdentity,
        "IssuerError": sigstore.oidc.IssuerError,
        "TimeoutError": TimeoutError,
        "ConnectionError": ConnectionError,
        "TokenExpiredMidFlow": _TokenExpiredMidFlow,
        "_TokenExpiredMidFlow": _TokenExpiredMidFlow,
    }
    if name in real_classes:
        cls = real_classes[name]
        try:
            return cls(msg) if msg else cls()
        except TypeError:
            try:
                return cls(msg)
            except Exception:  # noqa: BLE001
                return Exception(msg)
    # Private classes from sigstore internals.
    if name == "FulcioClientError":
        try:
            from sigstore._internal.fulcio.client import FulcioClientError
            return FulcioClientError(msg)
        except Exception:  # noqa: BLE001
            pass
    if name == "RekorClientError":
        try:
            from sigstore._internal.rekor import RekorClientError
            return RekorClientError(msg)
        except Exception:  # noqa: BLE001
            pass
    if name == "ExpiredCertificate":
        try:
            from sigstore._internal.fulcio.client import ExpiredCertificate
            return ExpiredCertificate(msg)
        except Exception:  # noqa: BLE001
            pass
    if name == "TimestampError":
        try:
            from sigstore._internal.timestamp import TimestampError
            return TimestampError(msg)
        except Exception:  # noqa: BLE001
            pass

    # Synthesize as ad-hoc subclass of Exception (NOT VerificationError) per
    # conftest.py:267-281 — exercises the catch-all branch.
    synth = type(name, (Exception,), {})
    return synth(msg)


_SIGN_FAILURE_MODES: dict[str, tuple[str, str]] = {
    "fulcio_503":                  ("FulcioClientError", "fulcio HTTP 503"),
    "fulcio_400_unknown_csr":      ("FulcioClientError", "fulcio HTTP 400 unknown CSR"),
    "fulcio_status_503":           ("FulcioClientError", "fulcio HTTP 503"),
    "rekor_502":                   ("RekorClientError", "rekor HTTP 502"),
    "rekor_timeout":               ("RekorClientError", "rekor timeout"),
    "rekor_404":                   ("RekorClientError", "rekor HTTP 404"),
    "oidc_unreachable":            ("IdentityError", "oidc unreachable"),
    "oidc_token_rejected":         ("IdentityError", "oidc token rejected"),
    "tuf_metadata_stale":          ("TUFError", "tuf metadata stale"),
    "expired_cert":                ("ExpiredCertificate", "cert expired"),
    "network_down":                ("NetworkError", "network unreachable"),
    "tuf_unavailable":             ("TUFError", "tuf unavailable"),
    "rekor_after_fulcio_503":      ("RekorClientError", "rekor unavailable after fulcio"),
    "tsa_unavailable_rekor_v2":    ("TimestampError", "tsa unavailable"),
    "oidc_token_expired_mid_flow": ("_TokenExpiredMidFlow", "token expired during signing"),
}


class _TestTrustRoot:
    def __init__(self, era: str = "current", **opts: Any) -> None:
        self.era = era
        self.opts = opts
        raw = era + ":" + json.dumps(opts, sort_keys=True, default=str)
        self.pin = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _TestCertificate:
    def __init__(self, *, curve: str, private_key: Any, certificate: Any) -> None:
        self.curve = curve
        self.private_key = private_key
        self.certificate = certificate


class _ParsedCheckpoint:
    def __init__(self, tree_size: int, root_hash: str, log_id: str) -> None:
        self.tree_size = tree_size
        self.root_hash = root_hash
        self.log_id = log_id


class _SignSpy:
    def __init__(self) -> None:
        self.fulcio_call_count = 0
        self.leaked_cert = False


class _FakeSigner:
    def __init__(self, factory: "_TestFactory", spy: _SignSpy) -> None:
        self.factory = factory
        self.spy = spy

    def sign_artifact(self, canonical_bytes: bytes) -> Bundle:
        opts = self.factory._sign_state
        failure = opts.get("failure_mode")
        raise_exc_name = opts.get("raise_sigstore_exception")
        raise_exc_msg = opts.get("raise_exc_msg", "")
        if raise_exc_name is None and failure in _SIGN_FAILURE_MODES:
            raise_exc_name, raise_exc_msg = _SIGN_FAILURE_MODES[failure]

        pre_fulcio_failures = {
            "oidc_unreachable", "oidc_token_rejected", "tuf_metadata_stale",
            "tuf_unavailable", "oidc_token_expired_mid_flow",
        }
        if failure in pre_fulcio_failures and raise_exc_name:
            raise _synthesize_exception(raise_exc_name, raise_exc_msg)

        if raise_exc_name:
            self.spy.fulcio_call_count += 1
            raise _synthesize_exception(raise_exc_name, raise_exc_msg)

        # CA-not-trusted: bundle minted, but tagged as unknown_ca so verify reports CERT_INVALID.
        if opts.get("ca") == "unknown-ca":
            self.spy.fulcio_call_count += 1
            opts2 = dict(opts)
            opts2["unknown_ca"] = True
            return self.factory._build_bundle(canonical_bytes, opts2)

        # Non-ASCII identity rejection.
        identity = opts.get("identity") or ""
        if opts.get("fulcio_rejects_non_ascii") and not identity.isascii():
            self.spy.fulcio_call_count += 1
            raise _synthesize_exception(
                "FulcioClientError",
                "Fulcio rejected non-ASCII identity in CSR",
            )

        # Cached-cert expiry on second call (sigstore-python #1729 regression).
        if opts.get("cert_expired_on_second_call"):
            key = id(opts)
            count = self.factory._sign_call_count_per_provider.get(key, 0)
            self.factory._sign_call_count_per_provider[key] = count + 1
            if count >= 1:
                self.spy.fulcio_call_count += 1
                raise _synthesize_exception(
                    "ExpiredCertificate",
                    "cached signing cert has expired mid-batch",
                )

        # simulate_tuf_race: must not leak raw exceptions across threads. Just succeed.
        self.spy.fulcio_call_count += 1
        return self.factory._build_bundle(canonical_bytes, opts)


class _FakeSigningContext:
    def __init__(self, factory: "_TestFactory", spy: _SignSpy) -> None:
        self.factory = factory
        self.spy = spy

    @contextlib.contextmanager
    def signer(self, identity_token: Any, *, cache: bool = True) -> Iterator[_FakeSigner]:
        yield _FakeSigner(self.factory, self.spy)


class _FakeVerifier:
    def __init__(self, factory: "_TestFactory") -> None:
        self.factory = factory

    def verify_artifact(self, input_: bytes, bundle: Bundle, policy: Any) -> None:
        opts = self.factory._verify_state
        exc_name = opts.get("raise_sigstore_exception")
        if exc_name:
            raise _synthesize_exception(exc_name, f"factory-injected {exc_name}")

        failure = opts.get("failure_mode")
        if failure:
            verify_failures = {
                "rekor_unreachable":              ("RekorClientError", "rekor unreachable"),
                "rekor_timeout":                  ("RekorClientError", "rekor timeout"),
                "tuf_unavailable_no_cached_root": ("TUFError", "tuf unavailable, no cached root"),
                "bundle_parse_error":             ("InvalidBundle", "bundle parse error"),
            }
            if failure in verify_failures:
                name, msg = verify_failures[failure]
                raise _synthesize_exception(name, msg)

        if opts.get("raise_in_verifier"):
            raise _synthesize_exception(
                "VerificationError", "factory-injected verifier internal error",
            )

        if opts.get("network") == "down":
            raise _synthesize_exception("NetworkError", "network down")

        try:
            sig_bytes = bytes(bundle._inner.message_signature.signature)
        except Exception:  # noqa: BLE001
            sig_bytes = b""

        meta = self.factory._registry.get(sig_bytes)
        if meta is not None:
            outcome = self.factory._evaluate_bundle(meta, bundle, input_)
            if outcome is not None:
                raise outcome
            return

        # Bundle not in registry: by default, just pass.
        return


class _FakeSigningContextClass:
    """Class-shaped stand-in for sigstore.sign.SigningContext."""

    def __init__(self, factory: "_TestFactory", spy: _SignSpy) -> None:
        self._factory = factory
        self._spy = spy

    def production(self) -> _FakeSigningContext:
        return _FakeSigningContext(self._factory, self._spy)


class _FakeVerifierClass:
    def __init__(self, factory: "_TestFactory", fake: _FakeVerifier) -> None:
        self._factory = factory
        self._fake = fake

    def production(self, *, offline: bool = False) -> _FakeVerifier:
        return self._fake

    def staging(self, *, offline: bool = False) -> _FakeVerifier:
        return self._fake

    def __call__(self, *, trusted_root=None) -> _FakeVerifier:
        return self._fake


class _FakeIdentityToken:
    def __init__(self, raw_token: str) -> None:
        self._raw = raw_token
        self.identity = "test-identity"
        self.issuer = "https://test-issuer.example"
        self.federated_issuer = self.issuer


def _FakeIdentityTokenFactory(raw_token: str) -> _FakeIdentityToken:
    return _FakeIdentityToken(raw_token)


class _TestFactory:
    """Test fixture for skein.signing per conftest.py:248-486 contract."""

    def __init__(self) -> None:
        self._registry: dict[bytes, dict] = {}
        self._sign_state: dict[str, Any] = {}
        self._verify_state: dict[str, Any] = {}
        self._sign_call_count_per_provider: dict[int, int] = {}
        self._verify_time_us: int | None = None
        self.fulcio_call_count: int = 0
        # Shared synthetic CA (one per process).
        self._ca_key = ec.generate_private_key(ec.SECP256R1())
        self._ca_cert = self._build_ca()

    def _reset(self) -> None:
        self._sign_state = {}
        self._verify_state = {}
        self._sign_call_count_per_provider = {}
        self._verify_time_us = None
        self.fulcio_call_count = 0

    # ------------------------------------------------------------------
    # CA + cert minting.
    # ------------------------------------------------------------------

    def _build_ca(self) -> x509.Certificate:
        subject = issuer = x509.Name(
            [x509.NameAttribute(x509.NameOID.COMMON_NAME, "skein-test-ca")]
        )
        now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(self._ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .sign(self._ca_key, hashes.SHA256())
        )

    def _mint_leaf_cert(
        self,
        *,
        private_key: Any,
        identity: str,
        issuer: str,
        san_type: str = "rfc822Name",
        extra_sans: list[tuple[str, str]] | None = None,
        not_before: datetime.datetime | None = None,
        not_after: datetime.datetime | None = None,
        issuer_oid_variant: str = "both",
        issuer_oid_v2: str | None = None,
        issuer_oid_legacy: str | None = None,
        include_san: bool = True,
        san_value: str | None = None,
        malformed_ia5: bool = False,
        include_sct: bool = True,
    ) -> x509.Certificate:
        # Add per-cert jitter so non-determinism tests see distinct validity
        # windows across sign() calls.
        now = datetime.datetime.now(datetime.timezone.utc)
        jitter_us = secrets.randbelow(120_000_000)  # up to ~120s
        nb = not_before or (now - datetime.timedelta(microseconds=300_000_000 + jitter_us))
        na = not_after or (now + datetime.timedelta(microseconds=600_000_000 + jitter_us))
        if nb.tzinfo is None:
            nb = nb.replace(tzinfo=datetime.timezone.utc)
        if na.tzinfo is None:
            na = na.replace(tzinfo=datetime.timezone.utc)

        san_value = san_value if san_value is not None else identity
        sans: list[x509.GeneralName] = []
        if include_san:
            sans.extend(self._build_san_entries(san_type, san_value, extra_sans, malformed_ia5))

        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([]))
            .issuer_name(self._ca_cert.subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(nb)
            .not_valid_after(na)
        )
        if include_san and sans:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(sans), critical=True,
            )

        def _utf8string_der(s: str) -> bytes:
            data = s.encode("utf-8")
            return bytes([0x0C, len(data)]) + data

        v2_value = issuer_oid_v2 if issuer_oid_v2 is not None else issuer
        legacy_value = issuer_oid_legacy if issuer_oid_legacy is not None else issuer
        if issuer_oid_variant in ("both", "v2"):
            builder = builder.add_extension(
                x509.UnrecognizedExtension(
                    x509.ObjectIdentifier(_OID_ISSUER_V2),
                    _utf8string_der(v2_value),
                ),
                critical=False,
            )
        if issuer_oid_variant in ("both", "legacy"):
            builder = builder.add_extension(
                x509.UnrecognizedExtension(
                    x509.ObjectIdentifier(_OID_ISSUER_LEGACY),
                    legacy_value.encode("utf-8"),
                ),
                critical=False,
            )
        # SCT extension is tracked via factory metadata (strip_sct / tamper_sct
        # etc.) rather than carried on the cert — cryptography validates the
        # SCT OID format and the synthetic certs aren't worth marshaling a real
        # PrecertificateSignedCertificateTimestamps payload.

        return builder.sign(self._ca_key, hashes.SHA256())

    def _build_san_entries(
        self,
        san_type: str,
        value: str,
        extra_sans: list[tuple[str, str]] | None,
        malformed_ia5: bool,
    ) -> list[x509.GeneralName]:
        pairs: list[tuple[str, str]] = []
        if san_type and value:
            pairs.append((san_type, value))
        if extra_sans:
            pairs.extend(extra_sans)
        out: list[x509.GeneralName] = []
        for t, v in pairs:
            if t == "rfc822Name":
                if malformed_ia5:
                    out.append(x509.OtherName(
                        x509.ObjectIdentifier("1.3.6.1.4.1.11129.2.4.99"),
                        b"\x16\x05" + b"\xff\xff\xff\xff\xff",
                    ))
                else:
                    out.append(x509.RFC822Name(v))
            elif t == "uniformResourceIdentifier":
                out.append(x509.UniformResourceIdentifier(v))
            elif t == "otherName_oid_57264_1_24":
                data = v.encode("utf-8")
                out.append(x509.OtherName(
                    x509.ObjectIdentifier(_OID_SIGNER_IDENTITY_OTHERNAME),
                    bytes([0x0C, len(data)]) + data,
                ))
            else:
                out.append(x509.RFC822Name(v))
        return out

    # ------------------------------------------------------------------
    # Bundle build.
    # ------------------------------------------------------------------

    def _build_bundle(self, canonical_bytes: bytes, opts: dict) -> Bundle:
        identity = opts.get("identity", "alice@example.com")
        issuer = opts.get("issuer") or (
            opts.get("provider").issuer if opts.get("provider") else "https://accounts.google.com"
        )
        san_type = opts.get("san_type", "rfc822Name")
        san_value = opts.get("san_value", identity)
        extra_sans = opts.get("extra_sans")
        include_san = opts.get("include_san", True)
        malformed_ia5 = opts.get("malformed_ia5", False)
        chain_length = opts.get("chain_length")
        issuer_oid_variant = opts.get("issuer_oid_variant", "both")
        issuer_oid_v2 = opts.get("issuer_oid_v2")
        issuer_oid_legacy = opts.get("issuer_oid_legacy")
        not_before = opts.get("not_before")
        not_after = opts.get("not_after")
        custom_cert = opts.get("custom_cert")
        include_sct = opts.get("include_sct", True)

        if custom_cert is not None:
            private_key = custom_cert.private_key
            leaf_cert = custom_cert.certificate
        else:
            private_key = ec.generate_private_key(ec.SECP256R1())
            leaf_cert = self._mint_leaf_cert(
                private_key=private_key,
                identity=identity,
                issuer=issuer,
                san_type=san_type,
                extra_sans=extra_sans,
                not_before=not_before,
                not_after=not_after,
                issuer_oid_variant=issuer_oid_variant,
                issuer_oid_v2=issuer_oid_v2,
                issuer_oid_legacy=issuer_oid_legacy,
                include_san=include_san,
                san_value=san_value,
                malformed_ia5=malformed_ia5,
                include_sct=include_sct,
            )

        digest = hashlib.sha256(canonical_bytes).digest()
        if isinstance(private_key, ec.EllipticCurvePrivateKey):
            signature = private_key.sign(
                digest, ec.ECDSA(Prehashed(hashes.SHA256())),
            )
        elif isinstance(private_key, ed25519.Ed25519PrivateKey):
            signature = private_key.sign(canonical_bytes)
        elif isinstance(private_key, rsa.RSAPrivateKey):
            signature = private_key.sign(
                digest, padding.PKCS1v15(), Prehashed(hashes.SHA256()),
            )
        else:
            signature = private_key.sign(canonical_bytes)

        log_id_bytes = opts.get("log_id_bytes") or secrets.token_bytes(32)
        # Jitter both log_index and tree_size for non-determinism.
        if "log_index" in opts:
            log_index = opts["log_index"]
        else:
            log_index = secrets.randbelow(1_000_000)
        if "tree_size" in opts:
            tree_size = opts["tree_size"]
        else:
            tree_size = log_index + 1 + secrets.randbelow(1_000)
        if tree_size <= log_index:
            tree_size = log_index + 1
        root_hash_bytes = opts.get("root_hash_bytes") or secrets.token_bytes(32)
        hashes_list_bytes = opts.get("inclusion_hashes_bytes") or [secrets.token_bytes(32)]
        checkpoint = opts.get("checkpoint") or self._make_checkpoint_envelope(
            tree_size, root_hash_bytes, log_id_bytes,
        )
        integrated_time_s = opts.get("integrated_time_s")
        if integrated_time_s is None:
            # Jitter integrated time for the K-A9 non-determinism test which
            # extracts the bundle's TSA "timestamp" field.
            integrated_time_s = int(time.time()) + secrets.randbelow(60)

        canonicalized_body = json.dumps({
            "apiVersion": "0.0.1",
            "kind": "hashedrekord",
            "spec": {
                "data": {"hash": {"algorithm": "sha256", "value": digest.hex()}},
                "signature": {
                    "content": base64.b64encode(signature).decode("ascii"),
                    "publicKey": {
                        "content": base64.b64encode(
                            leaf_cert.public_bytes(serialization.Encoding.PEM)
                        ).decode("ascii"),
                    },
                },
            },
        }).encode("utf-8")

        # Proto fields require base64-encoded bytes for ProtoBytes and string
        # forms for ProtoU64 (sigstore_models._core validators).
        inclusion_proof = rekor_v1.InclusionProof(
            log_index=str(log_index),
            root_hash=base64.b64encode(root_hash_bytes),
            tree_size=str(tree_size),
            hashes=[base64.b64encode(h) for h in hashes_list_bytes],
            checkpoint=rekor_v1.Checkpoint(envelope=checkpoint),
        )
        inclusion_promise = rekor_v1.InclusionPromise(
            signed_entry_timestamp=base64.b64encode(secrets.token_bytes(64)),
        )
        tlog_entry = rekor_v1.TransparencyLogEntry(
            log_index=str(log_index),
            log_id=common_v1.LogId(key_id=base64.b64encode(log_id_bytes)),
            kind_version=rekor_v1.KindVersion(kind="hashedrekord", version="0.0.1"),
            integrated_time=str(integrated_time_s),
            inclusion_promise=inclusion_promise,
            inclusion_proof=inclusion_proof,
            canonicalized_body=base64.b64encode(canonicalized_body),
        )

        tsa_list = opts.get("tsa_timestamps") or []
        timestamp_verification = bundle_v1.TimestampVerificationData(
            rfc3161_timestamps=[
                bundle_v1.RFC3161SignedTimestamp(signed_timestamp=base64.b64encode(t))
                for t in tsa_list
            ]
        )

        message_signature = common_v1.MessageSignature(
            message_digest=common_v1.HashOutput(
                algorithm=common_v1.HashAlgorithm.SHA2_256,
                digest=base64.b64encode(digest),
            ),
            signature=base64.b64encode(signature),
        )

        inner = bundle_v1.Bundle(
            media_type=Bundle.BundleType.BUNDLE_0_3.value,
            verification_material=bundle_v1.VerificationMaterial(
                certificate=common_v1.X509Certificate(
                    raw_bytes=base64.b64encode(
                        leaf_cert.public_bytes(serialization.Encoding.DER)
                    ),
                ),
                tlog_entries=[tlog_entry],
                timestamp_verification_data=timestamp_verification,
            ),
            message_signature=message_signature,
        )

        bundle = Bundle(inner)

        meta = {
            "canonical_bytes_signed": canonical_bytes,
            "identity": identity,
            "issuer": issuer,
            "cert_not_before_us": int(leaf_cert.not_valid_before_utc.timestamp() * 1_000_000),
            "cert_not_after_us": int(leaf_cert.not_valid_after_utc.timestamp() * 1_000_000),
            "integrated_time_us": int(integrated_time_s) * 1_000_000,
            "chain_length": chain_length,
            "san_type": san_type,
            "missing_san": not include_san,
            "malformed_ia5": malformed_ia5,
            "unknown_ca": opts.get("unknown_ca", False),
            "broken_chain": opts.get("broken_chain", False),
            "tampered_cert": False,
            "tampered_sig": False,
            "tampered_merkle": False,
            "tampered_checkpoint": False,
            "tampered_sct": False,
            "missing_rekor_proof": False,
            "missing_checkpoint": False,
            "missing_sct": False,
            "missing_tsa": False,
            "tsa_timestamps_us": [int(t) for t in opts.get("tsa_timestamps_us", [])],
            "checkpoint_root_mismatch": False,
            "checkpoint_tree_mismatch": False,
            "checkpoint_old_key": False,
            "checkpoint_malformed": False,
            "rekor_log_id_swapped": False,
            "digest_algorithm": "sha256",
            "splice_attack": False,
            "trust_root": opts.get("trust_root"),
            "issuer_oid_variant": issuer_oid_variant,
            "cross_sct": False,
            "untrusted_ct_log_sct": False,
            "future_dated": False,
            "dsse_envelope": opts.get("dsse", False),
        }
        self._registry[bytes(signature)] = meta
        return bundle

    def _make_checkpoint_envelope(self, tree_size: int, root_hash: bytes, log_id: bytes) -> str:
        sig_b = base64.b64encode(log_id[:4] + secrets.token_bytes(64)).decode("ascii")
        return (
            f"rekor.sigstore.dev - {int.from_bytes(log_id[:8], 'big')}\n"
            f"{tree_size}\n"
            f"{base64.b64encode(root_hash).decode('ascii')}\n"
            f"\n"
            f"— rekor.sigstore.dev {sig_b}\n"
        )

    # ------------------------------------------------------------------
    # Public factory API.
    # ------------------------------------------------------------------

    def install_sign_monkeypatch(self, monkeypatch, *, provider=None, **opts) -> _SignSpy:
        self._reset()
        opts["provider"] = provider
        # Map several "convenience" kwargs onto failure_mode strings.
        if "failure_mode" not in opts:
            if opts.get("fulcio_status") in (503, 500, 502, 504):
                opts["failure_mode"] = "fulcio_503"
            elif opts.get("fulcio_status") == 400:
                opts["failure_mode"] = "fulcio_400_unknown_csr"
            elif opts.get("rekor_status") in (502, 500, 503, 504):
                opts["failure_mode"] = "rekor_502"
            elif opts.get("rekor_status") == 404:
                opts["failure_mode"] = "rekor_404"
            elif opts.get("oidc_status") == "unreachable":
                opts["failure_mode"] = "oidc_unreachable"
            elif opts.get("oidc_status") == "token_rejected":
                opts["failure_mode"] = "oidc_token_rejected"
            elif opts.get("network") == "down":
                opts["failure_mode"] = "network_down"
        self._sign_state = opts
        spy = _SignSpy()

        fake_ctx = _FakeSigningContext(self, spy)
        # Patch the wrappers in skein.signing so production code calls our
        # fake. The boundary helpers (_build_production_signing_context,
        # _build_identity_token) are the only patch targets — we don't need
        # to touch sigstore.sign internals.
        monkeypatch.setattr(
            "skein.signing._build_production_signing_context",
            lambda: fake_ctx,
        )
        monkeypatch.setattr(
            "skein.signing._build_identity_token",
            _FakeIdentityTokenFactory,
        )
        return spy

    def install_verify_monkeypatch(self, monkeypatch, **opts) -> None:
        self._reset()
        self._verify_state = opts
        fake = _FakeVerifier(self)
        monkeypatch.setattr(
            "skein.signing._build_production_verifier",
            lambda *, offline=False: fake,
        )

    def make_staging_verifier(self) -> _FakeVerifier:
        return _FakeVerifier(self)

    # Bundle blob helpers -----------------------------------------------

    def make_bundle_blob(self, *, canonical_bytes, identity, issuer, **opts) -> str:
        opts = dict(opts)
        opts.update(identity=identity, issuer=issuer)
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_san(self, *, san_type, value, canonical_bytes, identity, issuer, **opts) -> str:
        opts = dict(opts)
        opts.update(
            identity=identity, issuer=issuer,
            san_type=san_type, san_value=value,
        )
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_multiple_sans(self, *, sans, canonical_bytes, identity, issuer, **opts) -> str:
        opts = dict(opts)
        opts.update(
            identity=identity, issuer=issuer,
            san_type=sans[0][0], san_value=sans[0][1],
            extra_sans=list(sans[1:]),
        )
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_missing_san(self, *, canonical_bytes, identity, issuer, **opts) -> str:
        opts = dict(opts)
        opts.update(identity=identity, issuer=issuer, include_san=False)
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_malformed_ia5string_san(self, *, canonical_bytes, identity, issuer, **opts) -> str:
        opts = dict(opts)
        opts.update(identity=identity, issuer=issuer, malformed_ia5=True)
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_rekor_inclusion(
        self, canonical_bytes, *, log_index, tree_size, leaf_hash=None,
        root_hash=None, hashes=None, checkpoint=None, log_id=None,
        identity="alice@example.com", issuer="https://accounts.google.com", **opts,
    ) -> str:
        opts = dict(opts)
        if log_id is not None:
            opts["log_id_bytes"] = (
                base64.b64decode(log_id) if isinstance(log_id, str) else log_id
            )
        if root_hash is not None:
            opts["root_hash_bytes"] = (
                base64.b64decode(root_hash) if isinstance(root_hash, str) else root_hash
            )
        if hashes is not None:
            opts["inclusion_hashes_bytes"] = [
                base64.b64decode(h) if isinstance(h, str) else h for h in hashes
            ]
        if checkpoint is not None:
            opts["checkpoint"] = checkpoint
        opts.update(
            identity=identity, issuer=issuer,
            log_index=log_index, tree_size=tree_size,
        )
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_chain_length(self, *, chain_length, canonical_bytes, identity, issuer, **opts) -> str:
        opts = dict(opts)
        opts.update(identity=identity, issuer=issuer, chain_length=chain_length)
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_broken_chain(self, *, canonical_bytes, identity, issuer, **opts) -> str:
        opts = dict(opts)
        opts.update(identity=identity, issuer=issuer, broken_chain=True)
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_dsse_envelope_bundle(self, canonical_bytes, **opts) -> str:
        opts = dict(opts)
        opts.update(
            identity="alice@example.com",
            issuer="https://accounts.google.com",
            dsse=True,
        )
        return self._build_bundle(canonical_bytes, opts).to_json()

    # Trust root --------------------------------------------------------

    def make_trust_root(self, **opts) -> _TestTrustRoot:
        return _TestTrustRoot(era="current", **opts)

    def make_era_trust_root(self, *, era: str, **opts) -> _TestTrustRoot:
        return _TestTrustRoot(era=era, **opts)

    def trust_root_pin(self, root: _TestTrustRoot) -> str:
        return root.pin

    def set_trust_root_pin(self, blob: str, pin_hash: str) -> str:
        # Embed a bundle-internal marker (SKEIN's SignatureBundle.trust_root_pin
        # remains authoritative; tests pass both when useful).
        obj = json.loads(blob)
        obj.setdefault("_skein_test", {})["trust_root_pin"] = pin_hash
        return json.dumps(obj)

    # Cert variants -----------------------------------------------------

    def make_cert_with_curve(self, curve: str) -> _TestCertificate:
        if curve == "P-256":
            key = ec.generate_private_key(ec.SECP256R1())
        elif curve == "P-384":
            key = ec.generate_private_key(ec.SECP384R1())
        elif curve == "Ed25519":
            key = ed25519.Ed25519PrivateKey.generate()
        elif curve == "RSA-2048":
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
        cert = self._mint_leaf_cert(
            private_key=key,
            identity="alice@example.com",
            issuer="https://accounts.google.com",
        )
        return _TestCertificate(curve=curve, private_key=key, certificate=cert)

    def set_leaf_cert(self, blob: str, cert: _TestCertificate) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        meta = self._registry.get(sig, {})
        canonical = meta.get("canonical_bytes_signed", b"")
        opts = {
            "identity": meta.get("identity", "alice@example.com"),
            "issuer": meta.get("issuer", "https://accounts.google.com"),
            "custom_cert": cert,
        }
        return self._build_bundle(canonical, opts).to_json()

    # Tamper helpers ----------------------------------------------------

    def _sig_from_obj(self, obj: dict) -> bytes:
        sig_b64 = obj.get("messageSignature", {}).get("signature")
        if not sig_b64:
            return b""
        try:
            return base64.b64decode(sig_b64)
        except Exception:  # noqa: BLE001
            return b""

    def _mark(self, blob: str, key: str, value: Any = True) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig][key] = value
        return blob

    def tamper_signature(self, blob: str) -> str:
        obj = json.loads(blob)
        sig_b64 = obj.get("messageSignature", {}).get("signature", "")
        if sig_b64:
            old = base64.b64decode(sig_b64)
            new = b"\x00" * len(old)
            obj["messageSignature"]["signature"] = base64.b64encode(new).decode("ascii")
            if old in self._registry:
                self._registry[old]["tampered_sig"] = True
                self._registry[new] = dict(self._registry[old])
                self._registry[new]["tampered_sig"] = True
        return json.dumps(obj)

    def set_digest_algorithm(self, blob: str, algo: str) -> str:
        obj = json.loads(blob)
        try:
            obj["messageSignature"]["messageDigest"]["algorithm"] = algo
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["digest_algorithm"] = algo
        return json.dumps(obj)

    def tamper_cert(self, blob: str) -> str:
        obj = json.loads(blob)
        cert_obj = obj.get("verificationMaterial", {}).get("certificate") or {}
        cert_b64 = cert_obj.get("rawBytes")
        if cert_b64:
            raw = bytearray(base64.b64decode(cert_b64))
            if len(raw) > 200:
                raw[150] ^= 0xFF
                cert_obj["rawBytes"] = base64.b64encode(bytes(raw)).decode("ascii")
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tampered_cert"] = True
        return json.dumps(obj)

    def tamper_merkle_hashes(self, blob: str) -> str:
        obj = json.loads(blob)
        try:
            proof = obj["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]
            if proof.get("hashes"):
                h = bytearray(base64.b64decode(proof["hashes"][0]))
                if h:
                    h[0] ^= 0xFF
                proof["hashes"][0] = base64.b64encode(bytes(h)).decode("ascii")
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tampered_merkle"] = True
        return json.dumps(obj)

    def strip_rekor_proof(self, blob: str) -> str:
        return self._mark(blob, "missing_rekor_proof", True)

    def strip_checkpoint(self, blob: str) -> str:
        return self._mark(blob, "missing_checkpoint", True)

    def set_rekor_hashes(self, blob: str, hashes: list[str]) -> str:
        obj = json.loads(blob)
        try:
            proof = obj["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]
            proof["hashes"] = hashes
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tampered_merkle"] = True
        return json.dumps(obj)

    def set_rekor_root_hash(self, blob: str, root_hash: str) -> str:
        obj = json.loads(blob)
        try:
            proof = obj["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]
            proof["rootHash"] = root_hash
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tampered_merkle"] = True
        return json.dumps(obj)

    def set_rekor_tree_size(self, blob: str, tree_size: int) -> str:
        obj = json.loads(blob)
        try:
            proof = obj["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]
            proof["treeSize"] = str(tree_size)
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tampered_merkle"] = True
        return json.dumps(obj)

    def alternate_rekor_log_id(self) -> str:
        return base64.b64encode(b"alternate-rekor-log" + b"\x00" * 13).decode("ascii")

    def swap_rekor_log_id(self, blob: str, new_log_id: str) -> str:
        obj = json.loads(blob)
        try:
            entry = obj["verificationMaterial"]["tlogEntries"][0]
            entry["logId"]["keyId"] = new_log_id
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["rekor_log_id_swapped"] = True
        return json.dumps(obj)

    def tamper_checkpoint_signature(self, blob: str) -> str:
        return self._mark(blob, "tampered_checkpoint", "signature")

    def checkpoint_with_different_root(self, blob: str) -> str:
        return self._mark(blob, "checkpoint_root_mismatch", True)

    def checkpoint_with_different_tree_size(self, blob: str) -> str:
        return self._mark(blob, "checkpoint_tree_mismatch", True)

    def checkpoint_signed_by_old_rekor_key(self, blob: str) -> str:
        return self._mark(blob, "checkpoint_old_key", True)

    def malformed_checkpoint(self, blob: str, *, kind: str) -> str:
        return self._mark(blob, "checkpoint_malformed", kind)

    def make_checkpoint(self, *, tree_size: int, root_hash: str, log_id: str | None = None) -> str:
        """Build a C2SP signed-note checkpoint.

        root_hash and log_id are embedded verbatim — parse_checkpoint_signed_note
        returns them in the same form.
        """
        origin = log_id if log_id else "rekor.sigstore.dev"
        sig_b = base64.b64encode(secrets.token_bytes(64)).decode("ascii")
        return (
            f"{origin}\n"
            f"{tree_size}\n"
            f"{root_hash}\n"
            f"\n"
            f"— {origin} {sig_b}\n"
        )

    def parse_checkpoint_signed_note(self, checkpoint: str) -> _ParsedCheckpoint:
        lines = checkpoint.strip().split("\n")
        if len(lines) < 3:
            raise ValueError("malformed checkpoint signed note")
        try:
            tree_size = int(lines[1])
            root_hash = lines[2]
        except (ValueError, IndexError) as exc:
            raise ValueError("malformed checkpoint signed note") from exc
        return _ParsedCheckpoint(tree_size=tree_size, root_hash=root_hash, log_id=lines[0])

    def future_dated_cert(self, blob: str) -> str:
        return self._mark(blob, "future_dated", True)

    def set_cert_validity(self, blob: str, *, not_before: int, not_after: int) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["cert_not_before_us"] = not_before
            self._registry[sig]["cert_not_after_us"] = not_after
        return json.dumps(obj)

    def set_rekor_integrated_time(self, blob: str, ts: int) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["integrated_time_us"] = ts
        return json.dumps(obj)

    def set_verify_time(self, ts: int) -> None:
        self._verify_time_us = ts

    def set_tsa_timestamp(self, blob: str, ts: int) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tsa_timestamps_us"] = [ts]
        return json.dumps(obj)

    def add_tsa_timestamp(self, blob: str, ts: int) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig].setdefault("tsa_timestamps_us", []).append(ts)
        return json.dumps(obj)

    def strip_tsa(self, blob: str) -> str:
        return self._mark(blob, "missing_tsa", True)

    def strip_sct(self, blob: str) -> str:
        return self._mark(blob, "missing_sct", True)

    def tamper_sct(self, blob: str) -> str:
        return self._mark(blob, "tampered_sct", True)

    def cross_sct(self, blob: str) -> str:
        return self._mark(blob, "cross_sct", True)

    def untrusted_ct_log_sct(self, blob: str) -> str:
        return self._mark(blob, "untrusted_ct_log_sct", True)

    def truncate(self, blob: str, n: int) -> str:
        return blob[:n]

    def bit_flip(self, blob: str, offset: int, bit: int = 0) -> str:
        if offset >= len(blob):
            return blob
        b = bytearray(blob.encode("utf-8", errors="replace"))
        b[offset] ^= (1 << bit)
        return b.decode("utf-8", errors="replace")

    def splice_rekor_entry(self, *, host: str, source: str) -> str:
        host_obj = json.loads(host)
        source_obj = json.loads(source)
        host_obj["verificationMaterial"]["tlogEntries"] = (
            source_obj["verificationMaterial"]["tlogEntries"]
        )
        sig = self._sig_from_obj(host_obj)
        if sig in self._registry:
            self._registry[sig]["splice_attack"] = True
        return json.dumps(host_obj)

    # Evaluation --------------------------------------------------------

    def _evaluate_bundle(self, meta: dict, bundle: Bundle, input_bytes: bytes) -> BaseException | None:
        # Digest-algorithm profile (sigstore-public-v1 SHA-256).
        if meta.get("digest_algorithm") not in (None, "sha256", "SHA2_256"):
            return _synthesize_exception("InvalidBundle", "non-SHA256 digest algorithm")

        if meta.get("dsse_envelope"):
            return _synthesize_exception("InvalidBundle", "DSSE envelope not supported in v0")

        if meta.get("splice_attack"):
            return _synthesize_exception("InvalidRekorEntry", "spliced Rekor entry")

        # Tampered cert / unknown CA / broken chain / chain_length 0.
        if meta.get("tampered_cert") or meta.get("unknown_ca") or meta.get("broken_chain"):
            return _synthesize_exception("InvalidMaterials", "cert chain validation failed")
        if meta.get("chain_length") == 0:
            return _synthesize_exception("InvalidMaterials", "self-signed leaf, no chain")

        # Tampered signature.
        if meta.get("tampered_sig"):
            return _SignatureInvalid("Signature is invalid for input")

        if meta.get("future_dated"):
            return _synthesize_exception("InvalidMaterials", "future-dated cert vs integrated time")

        nb_us = meta.get("cert_not_before_us")
        na_us = meta.get("cert_not_after_us")
        it_us = meta.get("integrated_time_us")
        if nb_us is not None and na_us is not None and it_us is not None:
            if it_us < nb_us or it_us > na_us:
                return _synthesize_exception(
                    "InvalidMaterials", "integrated time outside cert validity",
                )

        if meta.get("missing_tsa"):
            return _synthesize_exception(
                "InvalidBundle", "rekor v2 bundle missing TSA timestamp",
            )
        tsa_list = meta.get("tsa_timestamps_us") or []
        if tsa_list and nb_us is not None and na_us is not None:
            for ts in tsa_list:
                if ts < nb_us or ts > na_us:
                    return _synthesize_exception(
                        "InvalidMaterials", "TSA timestamp outside cert validity",
                    )
        if tsa_list and it_us is not None and na_us is not None:
            for ts in tsa_list:
                if abs(ts - it_us) > 1_000_000 and ts > na_us:
                    return _synthesize_exception(
                        "InvalidMaterials",
                        "rekor and TSA times disagree outside cert validity",
                    )

        if meta.get("missing_rekor_proof"):
            return _synthesize_exception("InvalidRekorEntry", "rekor inclusion proof missing")
        if meta.get("missing_checkpoint"):
            return _synthesize_exception("InvalidRekorEntry", "rekor checkpoint missing")
        if meta.get("tampered_merkle"):
            return _synthesize_exception("InvalidRekorEntry", "rekor merkle hashes tampered")
        if meta.get("rekor_log_id_swapped"):
            return _synthesize_exception("InvalidRekorEntry", "rekor log_id mismatch")
        if meta.get("checkpoint_root_mismatch"):
            return _synthesize_exception("InvalidRekorEntry", "checkpoint root mismatch")
        if meta.get("checkpoint_tree_mismatch"):
            return _synthesize_exception("InvalidRekorEntry", "checkpoint tree size mismatch")
        if meta.get("checkpoint_old_key"):
            return _synthesize_exception(
                "InvalidRekorEntry", "checkpoint signed by retired Rekor key",
            )
        if meta.get("checkpoint_malformed"):
            return _synthesize_exception("InvalidRekorEntry", "checkpoint malformed")
        if meta.get("tampered_checkpoint"):
            return _synthesize_exception("InvalidRekorEntry", "checkpoint signature tampered")

        if meta.get("missing_sct"):
            return _synthesize_exception("InvalidMaterials", "SCT missing")
        if meta.get("tampered_sct"):
            return _synthesize_exception("InvalidMaterials", "SCT tampered")
        if meta.get("cross_sct"):
            return _synthesize_exception("InvalidMaterials", "SCT for different cert")
        if meta.get("untrusted_ct_log_sct"):
            return _synthesize_exception("InvalidMaterials", "SCT from untrusted CT log")

        # Final: signature mismatch detection.
        if input_bytes != meta.get("canonical_bytes_signed"):
            return _SignatureInvalid("Signature is invalid for input")

        return None


# Module-level factory instance.
_test_factory: _TestFactory = _TestFactory()
