"""skein/signing.py — type definitions for the signing surface (Phase 2).

This module defines the Pydantic models, enum, and exception classes that the
Phase 2 test contract in tests/test_signing/ enforces. Phase 3 will add the
sign(), verify(), and verify_multi() function bodies.

Spec:
    brief-20260511-nbz4   Identity rev 5 (architectural spec)
    finding-20260511-kn5j RSP brief rev 3 (locked surface)
    finding-20260511-d3u6 spec clarifications (canonical names, EmptySignatureBundle,
                          MultiSignerBundle, OIDCProviderConfig 4 fields,
                          exception mapping)
    finding-20260513-w5hq addendum (RekorInclusionProof log_id)
"""

from __future__ import annotations

import base64
import binascii
from enum import Enum

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator


MIN_MICROSECOND_TIMESTAMP = 1_000_000_000_000_000


class OIDCProviderConfig(BaseModel):
    """OIDC provider configuration passed to sign().

    signing.py does not acquire tokens itself; the caller acquires the token
    out of band and passes it here. Per clarification 3.
    """

    issuer: str
    token: str
    provider_id: str | None = None
    # Microsecond UTC. Values below 1e15 are treated as seconds/milliseconds
    # confusion for the v0 contract, not valid signing-era timestamps.
    expires_at: int | None = Field(default=None, ge=MIN_MICROSECOND_TIMESTAMP)


class RekorInclusionProof(BaseModel):
    """Rekor v2 inclusion proof — independently verifiable Merkle witness.

    Sized so a downstream consumer (federation peer, archive auditor) can run
    the Merkle inclusion algorithm without trusting our verifier and without
    calling Rekor. Per clarification 4.
    """

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
    """Auxiliary cryptographic evidence attached to a verify result."""

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
    """Single-bundle verify outcome."""

    status: VerifyStatus
    issuer: str | None = None
    subject: str | None = None
    evidence: Evidence | None = None


class MultiVerifyResult(BaseModel):
    """Multi-signer verify outcome. overall == VERIFIED iff every result is VERIFIED."""

    results: list[VerifyResult] = Field(min_length=1)
    overall: VerifyStatus


class SignResult(BaseModel):
    """sign() return value. Superset of sigstore-python's bundle data.

    Carries the upstream library's bundle (serialized JSON) plus SKEIN's
    extracted convenience fields. Per clarification 1.
    """

    bundle_json: str
    issuer: str
    subject: str
    signing_timestamp: int = Field(ge=MIN_MICROSECOND_TIMESTAMP)
    evidence: Evidence


class SignatureBundle(BaseModel):
    """Folio-level wire shape; assembled by callers from sign() results.

    bundles is a list of canonical sigstore-bundle ProtoJSON strings (one per
    signer). canonical_bytes is the exact bytes every signer signed.
    """

    identity_scheme: str
    bundles: list[str] = Field(max_length=256)
    canonical_bytes: bytes
    canon_version: str = "knurl-1.0"
    trust_root_pin: str | None = None

    @field_serializer("canonical_bytes", when_used="json")
    def _serialize_canonical_bytes(self, v: bytes) -> str:
        """Emit canonical_bytes as standard base64 on the folio JSON wire."""
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
    """Domain failure: Fulcio, Rekor, or OIDC unreachable or reachable-but-failing.

    Caught by the offline-write queue (skein/sign_queue.py).
    """

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
    """Caller programming error: SignatureBundle constructed with empty bundles."""


class MultiSignerBundle(Exception):
    """Caller programming error: verify() called with multi-signer bundle.

    Per clarification 2: verify() requires len(bundles) == 1. Multi-signer
    bundles must go through verify_multi().
    """
