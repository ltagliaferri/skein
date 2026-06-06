"""Canonical serialization — the one producer of a folio's signed/hashed bytes.

A folio's identity is the hash of its canonical bytes, and signing must sign
EXACTLY those same bytes. So "the canonical bytes of a folio" must be ONE
function, shared by the hasher (:mod:`skein_next.identity`) and the
signer/verifier (:mod:`skein.signing`, wired in at the publish boundary). This
module owns that producer.

It is the seam the publish-path spec (brief-20260601-nqtj) and the signing brief
(brief-20260522-q8k0) both point at: ``folio_canonical_bytes`` is what gets
signed and verified, and ``identity.compute_folio_hash`` hashes the same bytes —
so the signed bytes and the hashed bytes can never drift. The field selection
and knurl serialization that used to live in ``identity.py`` now live here; that
move is byte-for-byte identical (the test suite is the guard), so existing hashes
are unchanged.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Union

from knurl import canon as _knurl_canon

# Fractional-seconds component of an ISO timestamp (the digits after ":SS."). The
# lookbehind anchors it to the seconds field so it never matches a stray dot.
_FRACTION_RE = re.compile(r"(?<=:\d\d)\.(\d+)")

# The five fields that constitute folio identity (canon sorts keys, so order here
# is just for readers). Kept explicit so callers and reviewers see the basis.
CANONICAL_FIELDS = ("type", "title", "content", "created_at", "created_by")

CreatedAt = Union[str, datetime, None]


def normalize_created_at(value: CreatedAt) -> Optional[str]:
    """Normalize any ``created_at`` representation to one canonical string.

    Accepts a ``datetime`` (naive or aware) or a string (isoformat, ``Z``-suffixed,
    space-separated SQLite form, or with an explicit offset). Returns a
    timezone-aware UTC isoformat string. ``None`` passes through as ``None``.

    Rules:
    - A naive datetime/string is assumed to already be in UTC.
    - An aware value is converted to UTC.
    - Sub-second precision is preserved (it is the corpus-wide uniqueness basis);
      whole-second inputs that denote the same instant collapse to one string.

    Raises ``ValueError`` if a string cannot be parsed as a timestamp,
    ``TypeError`` if the value is neither datetime, str, nor None.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = _parse_timestamp(value)
    else:
        raise TypeError(
            f"created_at must be a datetime, str, or None, got {type(value).__name__}"
        )

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _parse_timestamp(value: str) -> datetime:
    s = value.strip()
    # datetime.fromisoformat handles 'Z' natively only on 3.11+; normalize anyway
    # so behavior is identical across supported interpreters.
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    # Before 3.11, fromisoformat only accepts fractional seconds of exactly 3 or 6
    # digits (".1" raises); 3.11+ accepts any length. Normalize the fraction to
    # exactly 6 digits (right-pad, truncate beyond microsecond precision) so the
    # parse — and thus the canonical hash input — is identical across 3.10-3.12.
    s = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), s)
    try:
        return datetime.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"Unparseable created_at timestamp: {value!r}") from e


def folio_canonical_fields(fields: Mapping[str, Any]) -> dict:
    """The immutable five-field dict with ``created_at`` normalized.

    Only ``type, title, content, created_at, created_by`` participate; any other
    keys in ``fields`` are ignored.
    """
    return {
        "type": fields.get("type"),
        "title": fields.get("title"),
        "content": fields.get("content"),
        "created_at": normalize_created_at(fields.get("created_at")),
        "created_by": fields.get("created_by"),
    }


def folio_canonical_bytes(fields: Mapping[str, Any]) -> bytes:
    """The canonical bytes of a folio — the input to both hashing and signing."""
    return _knurl_canon.serialize(folio_canonical_fields(fields))


def thread_canonical_fields(
    from_id: Optional[str],
    to_id: Optional[str],
    type: Optional[str],
    weaver: Optional[str],
    created_at: CreatedAt,
    content: Optional[str],
) -> dict:
    """The six-field dict that constitutes a thread's identity (``created_at`` normalized)."""
    return {
        "from_id": from_id,
        "to_id": to_id,
        "type": type,
        "weaver": weaver,
        "created_at": normalize_created_at(created_at),
        "content": content,
    }


def thread_canonical_bytes(
    from_id: Optional[str],
    to_id: Optional[str],
    type: Optional[str],
    weaver: Optional[str],
    created_at: CreatedAt,
    content: Optional[str],
) -> bytes:
    """The canonical bytes of a thread."""
    return _knurl_canon.serialize(
        thread_canonical_fields(from_id, to_id, type, weaver, created_at, content)
    )
