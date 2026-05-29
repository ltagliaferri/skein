"""Content-hash store adapter for the web read surface (Slice 4).

The legacy templates were written against a ``JSONStore`` of pydantic ``Folio``/
``Site`` models: a folio has ``.folio_id``, ``.site_id``, a ``datetime``
``.created_at``, a ``.status``, etc. The new store speaks a different language —
identity is a ``sha256::<hex>`` content hash, a site is itself a folio, site
membership is a ``within`` thread, status is thread-derived, and ``created_at``
is stored as an ISO string. This adapter is the seam that translates one into
the other so the templates need not change:

- ``folio_id`` is the folio's content hash (also exposed as ``content_hash`` for
  the provenance block). URLs become ``/folio/sha256::<hex>``.
- ``site_id`` is the site's slug.
- ``created_at`` is parsed back into a timezone-aware ``datetime`` (the
  templates call ``.strftime``); a missing/garbage value becomes a sentinel
  minimum so sorting and formatting never crash.
- ``status`` is read from the latest ``status`` thread (batched), defaulting to
  ``"open"`` — never invented as a column.

The adapter opens its own store with ``check_same_thread=False`` and is created
fresh per request, so each request reads through an isolated connection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

from ..store import SkeinNextStore

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
_NON_REF_THREADS = {"within", "status"}


def _to_datetime(value: Optional[str]) -> datetime:
    """Parse a stored ISO ``created_at`` into an aware datetime (sentinel on miss)."""
    if not value:
        return _EPOCH
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return _EPOCH
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class FolioView:
    """A folio shaped like the legacy ``Folio`` model the templates expect."""

    __slots__ = (
        "folio_id", "content_hash", "type", "title", "content",
        "created_at", "created_by", "site_id", "status", "assigned_to",
    )

    def __init__(
        self,
        row: Dict,
        site_id: str = "",
        status: str = "open",
    ):
        content_hash = row["content_hash"]
        self.folio_id = content_hash
        self.content_hash = content_hash
        self.type = row.get("type") or "folio"
        self.title = row.get("title") or ""
        self.content = row.get("content") or ""
        self.created_at = _to_datetime(row.get("created_at"))
        self.created_by = row.get("created_by") or ""
        self.site_id = site_id
        self.status = status
        self.assigned_to = None  # not modeled in the new store


class SiteView:
    """A site shaped like the legacy ``Site`` model."""

    __slots__ = ("site_id", "purpose", "created_at", "created_by")

    def __init__(self, slug: str, folio: Optional[Dict]):
        self.site_id = slug
        self.purpose = (folio.get("content") or "") if folio else ""
        self.created_at = _to_datetime(folio.get("created_at") if folio else None)
        self.created_by = (folio.get("created_by") or "") if folio else ""


class ContentHashAdapter:
    """Presents the content-hash store behind the legacy web read interface."""

    def __init__(self, data_dir: Optional[Union[str, Path]] = None):
        self.store = SkeinNextStore(data_dir, check_same_thread=False)

    def close(self) -> None:
        self.store.close()

    # --- sites --------------------------------------------------------------

    def get_sites(self) -> List[SiteView]:
        return [SiteView(slug, folio) for slug, folio in self._site_folios()]

    def get_site(self, site_id: str) -> Optional[SiteView]:
        site_hash = self.store.resolve_slug(site_id)
        if not site_hash:
            return None
        return SiteView(site_id, self.store.get_folio(site_hash))

    def _site_folios(self):
        for slug, site_hash in self.store.list_slugs():
            yield slug, self.store.get_folio(site_hash)

    # --- folios -------------------------------------------------------------

    def get_folios(self, site_id: Optional[str] = None) -> List[FolioView]:
        """All folios, or one site's members. site_id is each folio's site slug.

        Status is filled in batch (latest status thread per folio). For the
        all-folios case the per-folio site slug comes from one membership join.
        """
        if site_id is not None:
            site_hash = self.store.resolve_slug(site_id)
            if not site_hash:
                return []
            rows = self.store.folios_in_site(site_hash)
            slug_of = {r["content_hash"]: site_id for r in rows}
        else:
            rows = self.store.list_folios(limit=1_000_000)
            slug_of = self.store.folio_site_slugs()
        statuses = self.store.latest_statuses([r["content_hash"] for r in rows])
        return [
            FolioView(
                r,
                site_id=slug_of.get(r["content_hash"], ""),
                status=statuses.get(r["content_hash"], "open"),
            )
            for r in rows
        ]

    def get_folio(self, folio_id: str) -> Optional[FolioView]:
        """Resolve a folio by content hash, falling back to the alias table.

        Internal links always carry the full content hash, but an imported
        legacy id may still be pasted in; the alias table resolves those.
        """
        row = self.store.get_folio(folio_id)
        if row is None:
            aliased = self.store.resolve_alias(folio_id)
            row = self.store.get_folio(aliased) if aliased else None
        if row is None:
            return None
        content_hash = row["content_hash"]
        slug = self.store.folio_site_slugs().get(content_hash, "")
        status = self.store.latest_statuses([content_hash]).get(content_hash, "open")
        return FolioView(row, site_id=slug, status=status)

    def search_folios(self, query: str, limit: int = 100) -> List[FolioView]:
        rows = self.store.search_folios(query, limit=limit)
        slug_of = self.store.folio_site_slugs()
        return [FolioView(r, site_id=slug_of.get(r["content_hash"], "")) for r in rows]

    # --- cross-references (rebuilt for hash identity) -----------------------

    def cross_refs(self, content_hash: str) -> List[FolioView]:
        """Folios linked to this one through the thread graph.

        Replaces the legacy regex-over-prose cross-ref scan, which can't work
        when identity is a content hash. Every non-membership, non-status thread
        touching this folio is followed to its other endpoint; a folio-hash
        endpoint resolves directly, a legacy-id endpoint resolves through the
        alias table. Deduplicated, membership/status edges excluded.
        """
        seen = set()
        refs: List[FolioView] = []
        edges = (
            self.store.get_threads(from_id=content_hash)
            + self.store.get_threads(to_id=content_hash)
        )
        for edge in edges:
            if edge["type"] in _NON_REF_THREADS:
                continue
            peer = edge["to_id"] if edge["from_id"] == content_hash else edge["from_id"]
            if not peer or peer == content_hash:
                continue
            target = peer if self.store.get_folio(peer) else self.store.resolve_alias(peer)
            if not target or target in seen:
                continue
            row = self.store.get_folio(target)
            if row:
                seen.add(target)
                refs.append(FolioView(row))
        return refs
