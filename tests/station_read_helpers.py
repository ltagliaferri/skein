"""Shared builders for the station READ-surface tests (station re-home Stage 4).

The skein_next web tests seeded their corpus through the fat-client authoring verbs
``Station.create_site`` / ``Station.post`` / ``Station.set_status`` — all DROP under the
re-home (the working skein authors over its 8001 API). This module rebuilds that corpus
DIRECTLY over the Stage-1 ``StationStore`` write path, mirroring those verbs faithfully so
the ported read tests keep their original assertions.

Three faithfulness points where the dropped verbs relied on skein_next's all-nullable
store and the strict-null ``StationStore`` (finding-20260709-18zn) does not:

- **Every thread carries a ``created_at``.** ``Station.post`` minted its ``within`` edge
  with a NULL ``created_at``; the station rejects that. The real Stage-3+ producer is a
  WORKBENCH publish whose ``threads.created_at`` is NOT NULL, so a conforming publish
  always carries one — these builders supply it (the within edge inherits the member
  folio's ``created_at``).
- **A status thread is a self-loop** — ``from_id == to_id == folio``, ``weaver`` = author,
  ``content`` = the status word — the exact shape ``Station.set_status`` wrote and the read
  side reduces via ``latest_statuses``. The skein_next web fixture took the nullable
  shortcut of a ``from_id``-less status edge; the self-loop is both strict-null-safe and
  the faithful station shape.
- **Site folios have a non-null ``created_by``.** A real site folio is a workbench publish
  with an author; the nullable fixture omitted it. The builder defaults it.

The corpus is written in plain write mode and the store is CLOSED on ``__exit__`` before
any read client opens it ``read_only`` — the read app's ``get_store`` opens ``mode=ro`` on
the existing corpus (``web/app.py`` ``get_store``), which requires the writer to have
committed and released first.
"""

from __future__ import annotations

from typing import Any, Optional

from skein.station_store import StationStore

# Default author/timestamp for rows the dropped verbs left null under the nullable store.
# Deterministic (no wall-clock) so a rebuilt corpus hashes reproducibly across runs.
_SEED_AUTHOR = "operator@example.com"
_SEED_TS = "2026-01-01T00:00:00Z"


class StationBuilder:
    """A minimal stand-in for the dropped ``skein_next.station.Station`` authoring verbs,
    writing the faithful station row shapes directly through ``StationStore``.

    Use as a context manager so the write store is committed and closed before a read
    client opens the corpus ``read_only``::

        with StationBuilder(data_dir) as st:
            st.create_site("proj", purpose="the project")
            a = st.post(type="finding", site="proj", title="A", content="body",
                        created_by="alice", created_at="2026-01-01T00:00:00Z")
    """

    def __init__(self, data_dir: Any):
        self.store = StationStore(data_dir, check_same_thread=False)

    def __enter__(self) -> "StationBuilder":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.store.close()

    def close(self) -> None:
        """Close the underlying write store (for tests that seed then read without a
        ``with`` block — commit is flushed by the store's per-write ``_maybe_commit``)."""
        self.store.close()

    def create_site(
        self,
        slug: str,
        purpose: Optional[str] = None,
        created_by: Optional[str] = None,
        created_at: str = _SEED_TS,
    ) -> str:
        """Mint a ``type=site`` folio for ``slug`` and bind the slug to it. Faithful to
        ``Station.create_site`` (site folio = its own genesis; the slug anchors on its
        hash), minus the concurrent-CAS ceremony a single-threaded test does not need."""
        site_hash = self.store.create_folio(
            {
                "type": "site",
                "title": slug,
                "content": purpose if purpose is not None else f"the {slug} site",
                "created_at": created_at,
                "created_by": created_by if created_by is not None else _SEED_AUTHOR,
            }
        )
        self.store.set_slug(slug, site_hash)
        return site_hash

    def post(
        self,
        type: str,
        site: str,
        title: str,
        content: Optional[str] = None,
        created_by: Optional[str] = None,
        created_at: str = _SEED_TS,
    ) -> str:
        """Create a folio and attach it ``within`` the (already-existing) ``site`` slug.
        The membership edge inherits the folio's ``created_at`` (strict-null-safe)."""
        site_hash = self.store.resolve_slug(site)
        if not site_hash:
            raise ValueError(f"unknown site slug: {site!r}")
        folio_hash = self.store.create_folio(
            {
                "type": type,
                "title": title,
                "content": content if content is not None else "",
                "created_at": created_at,
                "created_by": created_by if created_by is not None else _SEED_AUTHOR,
            }
        )
        self.store.save_thread(
            from_id=folio_hash, to_id=site_hash, type="within", created_at=created_at
        )
        return folio_hash

    def set_status(
        self,
        folio_hash: str,
        status: str,
        by: Optional[str] = None,
        created_at: str = _SEED_TS,
    ) -> str:
        """Write a status self-loop (``from_id == to_id == folio``) — the faithful station
        status-thread shape ``latest_statuses`` reduces. Returns the folio hash."""
        self.store.save_thread(
            from_id=folio_hash,
            to_id=folio_hash,
            type="status",
            weaver=by,
            content=status,
            created_at=created_at,
        )
        return folio_hash
