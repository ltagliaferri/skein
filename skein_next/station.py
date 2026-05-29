"""Station service layer over the content-hash store (Slice 3).

The store (:mod:`skein_next.store`) is pure storage: it hashes, inserts, and
queries rows. The *station* is the layer above it that speaks in the daily verbs
— post a folio into a site, read a folio by a short reference, list a site's
folios, search, walk a folio's thread graph, list sites. The CLI (Slice 3) and
the web read adapter (Slice 4) both sit on this one seam so the two surfaces can
never drift apart.

Three content-hash facts shape this layer:

- A folio's identity is its ``sha256::<hex>`` address. There is no human folio
  id, so :meth:`Station.resolve_ref` accepts a full address, a git-style short
  prefix, or a legacy id (via the alias table) and collapses them to one hash.
- A site is itself a ``type=site`` folio; membership is a ``within`` thread from
  the member folio to the site folio. ``folios_in_site`` walks that, not a
  column.
- Status is not a column either; it is thread-derived. This layer does not
  invent one — it exposes folios and threads and lets the caller read status off
  the graph.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .store import SkeinNextStore


class AmbiguousReference(Exception):
    """A short hash prefix matched more than one folio."""

    def __init__(self, prefix: str, matches: List[str]):
        self.prefix = prefix
        self.matches = matches
        super().__init__(
            f"reference {prefix!r} is ambiguous ({len(matches)} folios match)"
        )


class UnknownSite(Exception):
    """A site slug has no site folio in this station."""

    def __init__(self, slug: str):
        self.slug = slug
        super().__init__(f"no site named {slug!r}")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _short(content_hash: str, length: int = 12) -> str:
    """A git-style short address: ``sha256::`` + the first ``length`` hex chars."""
    if content_hash.startswith("sha256::"):
        return "sha256::" + content_hash[len("sha256::"):][:length]
    return content_hash[:length]


def _title_line(text: Optional[str], limit: int = 100) -> str:
    """The first non-blank line of ``text``, trimmed and length-capped."""
    if not text:
        return ""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(first) > limit:
        first = first[: limit - 1] + "…"
    return first


class Station:
    """The daily-verb service over a content-hash store. Context-manageable."""

    def __init__(self, data_dir: Optional[Union[str, Path]] = None):
        self.store = SkeinNextStore(data_dir)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Station":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- references ---------------------------------------------------------

    def resolve_ref(self, ref: str) -> Optional[str]:
        """Resolve a user-supplied reference to a full content hash, or ``None``.

        Resolution order, first hit wins:
        1. an exact ``sha256::<hex>`` address that is a stored folio;
        2. a legacy id present in the alias table;
        3. a unique ``sha256::`` short-hash prefix.

        Raises :class:`AmbiguousReference` when a prefix matches several folios.
        """
        if ref.startswith("sha256::") and self.store.get_folio(ref):
            return ref
        aliased = self.store.resolve_alias(ref)
        if aliased:
            return aliased
        if ref.startswith("sha256::"):
            matches = self.store.find_by_prefix(ref, limit=10)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise AmbiguousReference(ref, matches)
        return None

    # --- write --------------------------------------------------------------

    def post(
        self,
        type: str,
        site: str,
        title: str,
        content: Optional[str] = None,
        created_by: Optional[str] = None,
        created_at: Any = None,
    ) -> str:
        """Create a folio and attach it to ``site`` with a ``within`` thread.

        ``site`` is a slug that must already exist (raises :class:`UnknownSite`
        otherwise — the station never silently invents a site). Returns the new
        folio's content hash. Idempotent: re-posting identical fields into the
        same site yields the same hash and the same single membership edge.
        """
        site_hash = self.store.resolve_slug(site)
        if not site_hash:
            raise UnknownSite(site)
        folio_hash = self.store.create_folio(
            {
                "type": type,
                "title": title,
                "content": content,
                "created_at": created_at if created_at is not None else _now_utc(),
                "created_by": created_by,
            }
        )
        self.store.save_thread(from_id=folio_hash, to_id=site_hash, type="within")
        return folio_hash

    def create_site(
        self,
        slug: str,
        purpose: Optional[str] = None,
        created_by: Optional[str] = None,
        created_at: Any = None,
    ) -> str:
        """Ensure a ``type=site`` folio exists for ``slug``. Returns its hash.

        Idempotent on the slug: if the slug already resolves, the existing site
        folio is returned untouched and ``purpose``/``created_by`` are ignored.
        This matters because a site folio's hash includes its ``created_at`` —
        re-minting on every call would remap the slug to a fresh hash each time
        and orphan every ``within`` membership edge pointing at the old one.
        Changing a site's purpose is a deliberate update, not a re-create.
        """
        existing = self.store.resolve_slug(slug)
        if existing:
            return existing
        site_hash = self.store.create_folio(
            {
                "type": "site",
                "title": slug,
                "content": purpose,
                "created_at": created_at if created_at is not None else _now_utc(),
                "created_by": created_by,
            }
        )
        self.store.set_slug(slug, site_hash)
        return site_hash

    # --- read ---------------------------------------------------------------

    def get_folio(self, ref: str) -> Optional[Dict[str, Any]]:
        """Read a folio by any reference :meth:`resolve_ref` accepts."""
        content_hash = self.resolve_ref(ref)
        return self.store.get_folio(content_hash) if content_hash else None

    def list_folios(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        return self.store.list_folios(limit=limit, offset=offset)

    def folios_in_site(
        self, site: str, type: Optional[str] = None, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Folios that are members of ``site`` (via ``within`` threads).

        Raises :class:`UnknownSite` if the slug is unknown. Optionally filtered
        to one folio ``type``. Ordered by ``created_at`` to match the store.
        """
        site_hash = self.store.resolve_slug(site)
        if not site_hash:
            raise UnknownSite(site)
        members = self.store.get_threads(to_id=site_hash, type="within")
        folios: List[Dict[str, Any]] = []
        for edge in members:
            folio = self.store.get_folio(edge["from_id"])
            if folio and (type is None or folio.get("type") == type):
                folios.append(folio)
            if len(folios) >= limit:
                break
        folios.sort(key=lambda f: (f.get("created_at") or "", f["content_hash"]))
        return folios

    def search(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self.store.search_folios(query, limit=limit)

    def list_sites(self) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
        """All ``(slug, site_folio)`` pairs, ordered by slug.

        ``site_folio`` is ``None`` only if the slug table points at a hash with
        no folio row (a corrupt station) — surfaced rather than hidden.
        """
        return [
            (slug, self.store.get_folio(site_hash))
            for slug, site_hash in self.store.list_slugs()
        ]

    def get_site(self, slug: str) -> Optional[Dict[str, Any]]:
        site_hash = self.store.resolve_slug(slug)
        return self.store.get_folio(site_hash) if site_hash else None

    def thread_graph(self, ref: str) -> Optional[Dict[str, Any]]:
        """A folio's thread neighborhood: its outgoing and incoming edges.

        Returns ``None`` if the reference does not resolve. Each edge is the raw
        stored thread row plus a ``peer`` field describing the *other* endpoint:
        a resolved folio (when the peer is a stored content hash), or the raw
        actor/unresolved string the bridge kept verbatim. ``within`` membership
        edges are reported under their own key so callers can separate
        membership from substantive links.
        """
        content_hash = self.resolve_ref(ref)
        if not content_hash:
            return None
        focus = self.store.get_folio(content_hash)

        def describe_peer(peer_id: Optional[str]) -> Dict[str, Any]:
            if peer_id is None:
                return {"kind": "none", "id": None, "folio": None}
            folio = self.store.get_folio(peer_id)
            if folio:
                return {"kind": "folio", "id": peer_id, "folio": folio}
            return {"kind": "ref", "id": peer_id, "folio": None}

        outgoing, incoming, memberships = [], [], []
        for edge in self.store.get_threads(from_id=content_hash):
            row = dict(edge)
            row["peer"] = describe_peer(edge["to_id"])
            (memberships if edge["type"] == "within" else outgoing).append(row)
        for edge in self.store.get_threads(to_id=content_hash):
            row = dict(edge)
            row["peer"] = describe_peer(edge["from_id"])
            (memberships if edge["type"] == "within" else incoming).append(row)

        return {
            "folio": focus,
            "content_hash": content_hash,
            "outgoing": outgoing,
            "incoming": incoming,
            "memberships": memberships,
        }
