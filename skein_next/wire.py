"""Publish wire format (v0) for the client->instance publish path.

PROTOTYPE — unsigned. This module is the serialization seam between a client
station and an instance's ingress. It defines the on-the-wire shape of a publish
batch and the round-trip between stored folio/thread rows and that shape.

It is deliberately the ONE place the wire shape lives, because this is where
signing will later attach: a ``signature_bundle`` per folio (and the
``folio_canonical_bytes`` it signs over) wraps around the folio dicts produced
here. Keeping serialization in a single module means the signing boundary has a
single seam to hook, not a shape scattered across the publish CLI and the
ingress route.

Shape of a publish batch (JSON)::

    {
      "protocol": "skein-publish/v0",
      "folios":  [{content_hash, type, title, content, created_at, created_by}, ...],
      "threads": [{thread_hash, from_id, to_id, type, weaver, created_at, content}, ...]
    }

A site travels as an ordinary ``type=site`` folio, and membership as the
``within`` thread to it — so site identity (its content hash) is preserved
across the boundary rather than re-minted on the instance. The instance learns
the human slug for a received site from the slug carried alongside (see
``site_slugs`` on the batch); the slug is instance-local convenience, not part
of any content hash.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from .identity import compute_folio_hash, compute_thread_hash

PROTOCOL = "skein-publish/v0"

# The folio fields that cross the wire: the content hash (so the receiver can
# verify it) plus exactly the five canonical fields that produce it. Nothing
# else — publish-state and (later) signature_bundle are overlay, carried
# separately, never folded into a folio's identity.
FOLIO_WIRE_FIELDS = ("type", "title", "content", "created_at", "created_by")
THREAD_WIRE_FIELDS = ("from_id", "to_id", "type", "weaver", "created_at", "content")


def folio_to_wire(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Serialize a stored folio row to its wire dict (content_hash + canon fields)."""
    wire = {"content_hash": row["content_hash"]}
    wire.update({f: row.get(f) for f in FOLIO_WIRE_FIELDS})
    return wire


def thread_to_wire(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Serialize a stored thread row to its wire dict (thread_hash + canon fields)."""
    wire = {"thread_hash": row["thread_hash"]}
    wire.update({f: row.get(f) for f in THREAD_WIRE_FIELDS})
    return wire


def build_batch(
    folios: List[Mapping[str, Any]],
    threads: List[Mapping[str, Any]],
    site_slugs: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Assemble a publish batch from stored folio/thread rows.

    ``site_slugs`` maps a site folio's content hash to its human slug, so the
    instance can register the slug for any ``type=site`` folio it receives.
    """
    return {
        "protocol": PROTOCOL,
        "folios": [folio_to_wire(f) for f in folios],
        "threads": [thread_to_wire(t) for t in threads],
        "site_slugs": dict(site_slugs or {}),
    }


def recompute_folio_hash(wire_folio: Mapping[str, Any]) -> str:
    """The content hash a received folio's canonical fields actually produce."""
    return compute_folio_hash(wire_folio)


def recompute_thread_hash(wire_thread: Mapping[str, Any]) -> str:
    """The content hash a received thread's canonical fields actually produce."""
    return compute_thread_hash(
        from_id=wire_thread.get("from_id"),
        to_id=wire_thread.get("to_id"),
        type=wire_thread.get("type"),
        weaver=wire_thread.get("weaver"),
        created_at=wire_thread.get("created_at"),
        content=wire_thread.get("content"),
    )


def folio_reject_reason(wire_folio: Mapping[str, Any]) -> Optional[str]:
    """Why a folio should be rejected, or ``None`` if it is intact.

    This is the integrity check the unsigned prototype leans on: even with no
    signature, a folio whose body was altered in transit will not re-hash to its
    claimed address. The check is TOTAL — fields that cannot even be hashed
    (e.g. an unparseable or non-string ``created_at``) are rejected as
    ``"invalid fields"`` rather than raising, so a malformed batch never 500s
    the ingress. Signing later upgrades this from "the content is intact" to
    "the content is intact AND authored by X".
    """
    try:
        recomputed = recompute_folio_hash(wire_folio)
    except (ValueError, TypeError):
        return "invalid fields"
    if wire_folio.get("content_hash") != recomputed:
        return "hash mismatch"
    return None


def thread_reject_reason(wire_thread: Mapping[str, Any]) -> Optional[str]:
    """Why a thread should be rejected, or ``None`` if it is intact (total, as above)."""
    try:
        recomputed = recompute_thread_hash(wire_thread)
    except (ValueError, TypeError):
        return "invalid fields"
    if wire_thread.get("thread_hash") != recomputed:
        return "hash mismatch"
    return None


def folio_hash_ok(wire_folio: Mapping[str, Any]) -> bool:
    """True iff the folio is intact (no reject reason)."""
    return folio_reject_reason(wire_folio) is None


def thread_hash_ok(wire_thread: Mapping[str, Any]) -> bool:
    """True iff the thread is intact (no reject reason)."""
    return thread_reject_reason(wire_thread) is None
