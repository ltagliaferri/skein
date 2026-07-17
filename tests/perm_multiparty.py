"""Workstream 0 — the multi-party permission harness (rung 2, brief-20260716-93t3 §5).

Fact 1 of the plan: *no permission test that reaches ``ingest()`` has ever driven a
SECOND identity.* Every such test uses ``ok_verifier`` (station_publish_helpers.py), a
constant function fixing the signer to ALICE — so "party 2" exists only as attribution
rows written straight into the DB, and the identity that authorizes is always the
identity that signs. The model's purpose is "may THIS signer act on THAT thing," and it
has been exercised with one signer.

This harness closes that. Identity flows from the VERIFIER, not the manifest bundle:
``verify_wire_manifest`` derives the admitting ``(issuer, subject)`` solely from
``result.results[0]`` (sign.py:365). So a per-party verifier closure makes party 2
GENUINELY drive authorization through the real transaction, the staged graph, and the
live tier/grant reads.

Rung 2 (the substrate table, §4): ``ingest()`` + a PARAMETERIZED verifier. Party 2
drives a real batch; the binding is read live; attribution is written. Identity is
ASSERTED by the closure, not derived from crypto (that is rung 3+). Below rung 5 party 2
is a synthetic SAN, so the honest claim is *"a real second SIGNATURE has driven the
model,"* not *"a real second party."*
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from skein import signing, wire
from skein.ingress import ingest
from tests import station_publish_helpers as h
from tests.station_publish_helpers import I

# --- the cast (plan §5 "the free win", §7 "Cast") ---------------------------
# Two principals, deliberately: binding ONE party globally would rotate the blind spot
# (no test would then exercise a genuinely UNBOUND signer). So BOB is the bound-but-
# unauthorized probe and STRANGER is the unbound probe.
ALICE = h.ALICE                     # the fixture owner (bound originator)
BOB = "bob@example.com"             # bound originator; NO ownership/tier/grant on ALICE's lineage
STRANGER = "stranger@example.com"   # never bound — the binding probe
STEWARD = "steward@example.com"
ADMIN = "admin@example.com"
OPERATOR = "op@example.com"


def make_verifier(subject: str, issuer: str = I) -> "signing.Verifier":
    """A rung-2 verifier fixed to ``subject`` — the lever Fact 1 was missing.

    ``verify_wire_manifest`` reads the admitting identity from ``result.results[0]``
    (sign.py:365), so THIS closure — not the manifest bundle — decides who signed.
    Passing ``make_verifier(BOB)`` to ``ingest`` makes BOB the authorizing signer for
    that batch, through the real ON path.
    """
    def _verify(canonical_bytes: bytes, bundle: Any) -> "signing.MultiVerifyResult":
        return signing.MultiVerifyResult(
            results=[signing.VerifyResult(
                status=signing.VerifyStatus.VERIFIED, issuer=issuer, subject=subject,
            )],
            overall=signing.VerifyStatus.VERIFIED,
        )
    return _verify


def bind_party(
    instance, subject: str, role: str = "originator", *,
    issuer: str = I, vouched_by: str = OPERATOR,
) -> None:
    """Bind ``subject`` at ``role`` — the ``account_bindings`` row the live
    ``get_binding`` reads inside the ingest transaction."""
    instance.store.add_binding(
        issuer, subject, role=role,
        vouched_by_issuer=issuer, vouched_by_subject=vouched_by,
    )


def publish_as(
    instance, subject: str,
    folios: Sequence[Mapping[str, Any]],
    threads: Sequence[Mapping[str, Any]],
    *,
    site_slugs: Optional[Mapping[str, str]] = None,
    require_signed: bool = True,
    issuer: str = I,
) -> Dict[str, Any]:
    """Publish ``folios``/``threads`` through the real ``ingest()`` AS ``subject``.

    The manifest is signed by and verified as ``subject`` (both agree, though only the
    verifier's identity is load-bearing — see :func:`make_verifier`). Returns the ingest
    ack: ``{accepted, existing, rejected, threads:{accepted, existing, rejected}}``.
    """
    batch = wire.build_batch(list(folios), list(threads), site_slugs)
    batch["manifest_signature"] = h.manifest_over(
        list(folios), list(threads), signer=h.make_signer(issuer, subject),
    )
    return ingest(
        instance, batch,
        verifier=make_verifier(subject, issuer), require_signed=require_signed,
    )
