"""Shared builders for the station-ingress round-trip tests (station re-home Stage 3).

The skein_next ingress tests authored client content via ``Station.create_site`` /
``Station.post`` (the fat-client authoring verbs, DROP under the re-home) + skein_next's
``publish.collect_publish_set``. Those verbs are NOT re-homed — the working skein authors
over its 8001 API. So these tests build the publish set DIRECTLY from field dicts, exactly
as ``tests/test_station_store.py`` builds its corpus: compute the content hash from the
canonical fields, assemble the wire rows, and hand them to ``skein.wire.build_batch``.

One deliberate divergence from the skein_next authoring path: the ``within`` membership
edge carries a ``created_at``. skein_next's ``create_site`` minted within-edges with a
NULL ``created_at`` (its store was all-nullable); the Stage-1 ``StationStore`` narrows to
strict-null (thread ``created_at`` NOT NULL, finding-20260709-18zn). This is SAFE because
the real Stage-3+ producer is a WORKBENCH publish, whose ``threads.created_at`` column is
NOT NULL — so a conforming publish always carries it. These builders mimic that producer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from skein import profile, signing
from skein import sign as sign_mod
from skein.identity import compute_folio_hash, compute_thread_hash

# The verified identity the fake signer/verifier agree on across the suite.
I = "https://accounts.google.com"
ALICE = "alice@example.com"


def folio(
    type: str,
    title: str,
    content: str,
    created_at: str,
    created_by: str = "t",
) -> Dict[str, Any]:
    """A stored-folio row (content_hash + the five canonical fields)."""
    fields = {
        "type": type,
        "title": title,
        "content": content,
        "created_at": created_at,
        "created_by": created_by,
    }
    return {"content_hash": compute_folio_hash(fields), **fields}


def within(from_id: str, to_id: str, created_at: str) -> Dict[str, Any]:
    """A ``within`` membership edge (thread_hash + canonical fields). ``created_at``
    is REQUIRED — the strict-null station rejects a null-created_at thread."""
    th = compute_thread_hash(
        from_id=from_id, to_id=to_id, type="within",
        weaver=None, created_at=created_at, content=None,
    )
    return {
        "thread_hash": th, "from_id": from_id, "to_id": to_id,
        "type": "within", "weaver": None, "created_at": created_at, "content": None,
    }


def specs_set(
    *,
    site_title: str = "Public specs",
    finding_title: str = "Design Overview",
    finding_content: str = "body",
    created_by: str = "t",
    finding_created_by: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
    """The canonical publish set: a ``specs`` site folio + one finding + the within edge.

    Returns ``(folios, threads, site_slugs)`` ready for ``wire.build_batch`` — the
    stand-in for ``collect_publish_set(client, site="specs")``."""
    site = folio("site", site_title, "the specs site", "2026-01-01T00:00:00+00:00", created_by)
    fnd = folio(
        "finding", finding_title, finding_content, "2026-01-02T00:00:00+00:00",
        finding_created_by or created_by,
    )
    edge = within(fnd["content_hash"], site["content_hash"], "2026-01-03T00:00:00+00:00")
    return [site, fnd], [edge], {site["content_hash"]: "specs"}


def make_signer(issuer: str = I, subject: str = ALICE):
    """A fake manifest signer (no crypto): stamps the manifest profile so the strict
    verifier's profile gate passes and the fake verifier branch is what's under test."""
    def _s(canonical_bytes: bytes) -> "sign_mod.SignedResult":
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, canonical_bytes)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)
    return _s


def ok_verifier(canonical_bytes: bytes, bundle: Any) -> "signing.MultiVerifyResult":
    return signing.MultiVerifyResult(
        results=[signing.VerifyResult(status=signing.VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
        overall=signing.VerifyStatus.VERIFIED,
    )


def bad_verifier(canonical_bytes: bytes, bundle: Any) -> "signing.MultiVerifyResult":
    return signing.MultiVerifyResult(
        results=[signing.VerifyResult(status=signing.VerifyStatus.SIGNATURE_MISMATCH)],
        overall=signing.VerifyStatus.SIGNATURE_MISMATCH,
    )


def manifest_over(
    folios: List[Mapping[str, Any]],
    threads: List[Mapping[str, Any]],
    signer=None,
    addresses_override: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """A ``manifest_signature`` covering every folio + thread hash (or an override set)."""
    addrs = addresses_override or (
        [f["content_hash"] for f in folios] + [t["thread_hash"] for t in threads]
    )
    return sign_mod.sign_manifest(addrs, signer or make_signer())
