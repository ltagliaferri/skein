"""Content-hash identity for new-skein folios.

Folio identity is the content hash of five canonical fields:
``type, title, content, created_at, created_by``. This is the exact field set
and knurl canonicalization used by the legacy store (``skein/storage.py``
``compute_folio_hash``) — adopted deliberately, not re-guessed.

The one place identity can silently break is ``created_at``: the same logical
instant arrives over the wire in incompatible encodings (timezone-naive, aware
with an offset, ``Z``-suffixed, a raw SQLite string, whole-second resolution).
``normalize_created_at`` collapses all of them to one canonical representation —
timezone-aware UTC isoformat — *before* the value enters the canonical bytes, so
a folio hashes identically no matter how its timestamp was expressed. See
open-call #1 in brief-20260529-i6fy.

Address spelling note: the stored hash uses the ``sha256::<hex>`` address form.
The addressing RSP (brief-20260529-0yjl) owns the final spelling; this module
just stays consistent with it. The underlying SHA-256 digest is identical to the
legacy ``folio:sha256:<hex>`` digest — only the prefix framing differs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Union

from knurl import canon, hash as knurl_hash

# The five fields that constitute folio identity, in no particular order
# (canon sorts keys). Kept explicit so callers and reviewers can see the basis.
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

    Raises ``ValueError`` if a string cannot be parsed as a timestamp.
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
    try:
        return datetime.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"Unparseable created_at timestamp: {value!r}") from e


def _canonical_fields(fields: Mapping[str, Any]) -> dict:
    """Build the immutable five-field dict with created_at normalized."""
    return {
        "type": fields.get("type"),
        "title": fields.get("title"),
        "content": fields.get("content"),
        "created_at": normalize_created_at(fields.get("created_at")),
        "created_by": fields.get("created_by"),
    }


def _address(content: str) -> str:
    """Hash a canonical string and return it in the ``sha256::<hex>`` address form."""
    # knurl returns 'sha256:<hex>'; reframe to the address form 'sha256::<hex>'.
    digest = knurl_hash.compute(content)
    return "sha256::" + digest.split(":", 1)[1]


def compute_folio_hash(fields: Mapping[str, Any]) -> str:
    """Compute a folio's content hash from its five canonical fields.

    Only ``type, title, content, created_at, created_by`` participate; any other
    keys in ``fields`` are ignored. ``created_at`` is normalized first.
    """
    canonical = canon.serialize(_canonical_fields(fields))
    return _address(canonical.decode("utf-8"))


def compute_thread_hash(
    from_id: Optional[str],
    to_id: Optional[str],
    type: Optional[str],
    weaver: Optional[str],
    created_at: CreatedAt,
    content: Optional[str],
) -> str:
    """Compute a thread's content hash from its canonical fields.

    Threads are content-addressed like folios so save is idempotent. ``created_at``
    is normalized identically.
    """
    canonical = canon.serialize(
        {
            "from_id": from_id,
            "to_id": to_id,
            "type": type,
            "weaver": weaver,
            "created_at": normalize_created_at(created_at),
            "content": content,
        }
    )
    return _address(canonical.decode("utf-8"))
