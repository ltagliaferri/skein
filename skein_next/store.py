"""Content-hash-native SQLite store for new-skein (Slice 1).

A folio's identity *is* its content hash — there is no human folio_id. Threads
and aliases hang off the same content-addressed model. This module owns storage
and querying only; it does not classify thread endpoints (folio-hash vs actor id)
— that is the import bridge's job in Slice 2.

Data lives under ``.skein-next/`` (separate from legacy ``.skein/``, which is
never touched). The schema deliberately omits the legacy ``status``/``assigned_to``
folio columns — new-skein is thread-native from the start.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from .identity import compute_folio_hash, compute_thread_hash, normalize_created_at

DEFAULT_DATA_DIR = Path(".skein-next")
DB_FILENAME = "store.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS folios (
    content_hash TEXT PRIMARY KEY,
    type         TEXT,
    created_at   TEXT,
    created_by   TEXT,
    title        TEXT,
    content      TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    thread_hash TEXT PRIMARY KEY,
    from_id     TEXT,
    to_id       TEXT,
    type        TEXT,
    weaver      TEXT,
    created_at  TEXT,
    content     TEXT
);

CREATE TABLE IF NOT EXISTS aliases (
    legacy_id    TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slugs (
    slug         TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_from ON threads(from_id);
CREATE INDEX IF NOT EXISTS idx_threads_to   ON threads(to_id);
CREATE INDEX IF NOT EXISTS idx_threads_type ON threads(type);
CREATE INDEX IF NOT EXISTS idx_folios_created_at ON folios(created_at);
"""


class SkeinNextStore:
    """Content-hash-native folio/thread/alias store backed by SQLite."""

    def __init__(self, data_dir: Optional[Union[str, Path]] = None):
        self.data_dir = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / DB_FILENAME
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._in_batch = False

    def close(self) -> None:
        self.conn.close()

    # --- transactions -------------------------------------------------------

    def _maybe_commit(self) -> None:
        """Commit immediately unless inside a batch transaction."""
        if not self._in_batch:
            self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator["SkeinNextStore"]:
        """Batch many writes into a single commit.

        Per-write commits make a 10k-row import I/O-bound (one fsync per row).
        Inside this context, ``create_folio``/``save_thread``/``set_alias``/
        ``set_slug`` defer their commit; the whole batch commits once on exit,
        or rolls back if the block raises. Not re-entrant.
        """
        if self._in_batch:
            raise RuntimeError("transaction() is not re-entrant")
        self._in_batch = True
        try:
            yield self
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        finally:
            self._in_batch = False

    def __enter__(self) -> "SkeinNextStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- folios -------------------------------------------------------------

    def create_folio(self, fields: Mapping[str, Any]) -> str:
        """Normalize, hash, and idempotently insert a folio. Returns its hash.

        Inserting the same logical folio twice (including the same instant under
        different created_at encodings) yields one row and the same hash.
        """
        content_hash = compute_folio_hash(fields)
        normalized_created_at = normalize_created_at(fields.get("created_at"))
        self.conn.execute(
            """
            INSERT OR IGNORE INTO folios
                (content_hash, type, created_at, created_by, title, content)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                content_hash,
                fields.get("type"),
                normalized_created_at,
                fields.get("created_by"),
                fields.get("title"),
                fields.get("content"),
            ),
        )
        self._maybe_commit()
        return content_hash

    def get_folio(self, content_hash: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM folios WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return dict(row) if row else None

    def list_folios(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM folios
            ORDER BY created_at, content_hash
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_folios(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Folios whose title or content contains ``query`` (case-insensitive).

        A plain substring match — the daily-driver search verb, not a ranked
        index. ``query`` is matched literally; SQL ``LIKE`` wildcards in it are
        escaped so a user searching for ``50%`` or ``a_b`` finds those strings.
        """
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.conn.execute(
            """
            SELECT * FROM folios
            WHERE title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\'
            ORDER BY created_at, content_hash
            LIMIT ?
            """,
            (like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_by_prefix(self, prefix: str, limit: int = 10) -> List[str]:
        """Content hashes beginning with ``prefix`` (git-style short-hash lookup).

        ``prefix`` is matched literally against the full stored ``sha256::<hex>``
        address; ``LIKE`` metacharacters are escaped. Returns up to ``limit``
        matches so a caller can detect (and reject) an ambiguous prefix.
        """
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.conn.execute(
            """
            SELECT content_hash FROM folios
            WHERE content_hash LIKE ? ESCAPE '\\'
            ORDER BY content_hash
            LIMIT ?
            """,
            (like, limit),
        ).fetchall()
        return [r["content_hash"] for r in rows]

    # --- threads ------------------------------------------------------------

    def save_thread(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
        weaver: Optional[str] = None,
        created_at: Any = None,
        content: Optional[str] = None,
    ) -> str:
        """Idempotently store a thread edge. Returns its content hash.

        ``from_id``/``to_id`` may hold a folio content-hash or an actor/external
        id; the store does not classify them.
        """
        thread_hash = compute_thread_hash(
            from_id, to_id, type, weaver, created_at, content
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO threads
                (thread_hash, from_id, to_id, type, weaver, created_at, content)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_hash,
                from_id,
                to_id,
                type,
                weaver,
                normalize_created_at(created_at),
                content,
            ),
        )
        self._maybe_commit()
        return thread_hash

    def get_threads(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query threads by any combination of from_id, to_id, and type."""
        clauses = []
        params: List[Any] = []
        if from_id is not None:
            clauses.append("from_id = ?")
            params.append(from_id)
        if to_id is not None:
            clauses.append("to_id = ?")
            params.append(to_id)
        if type is not None:
            clauses.append("type = ?")
            params.append(type)

        sql = "SELECT * FROM threads"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, thread_hash"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # --- aliases ------------------------------------------------------------

    def set_alias(self, legacy_id: str, content_hash: str) -> None:
        """Map a legacy id to a content hash (upsert; last write wins)."""
        self.conn.execute(
            """
            INSERT INTO aliases (legacy_id, content_hash)
            VALUES (?, ?)
            ON CONFLICT(legacy_id) DO UPDATE SET content_hash = excluded.content_hash
            """,
            (legacy_id, content_hash),
        )
        self._maybe_commit()

    def resolve_alias(self, legacy_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content_hash FROM aliases WHERE legacy_id = ?", (legacy_id,)
        ).fetchone()
        return row["content_hash"] if row else None

    # --- slugs --------------------------------------------------------------

    def set_slug(self, slug: str, content_hash: str) -> None:
        """Map a site slug to its site-folio content hash (upsert)."""
        self.conn.execute(
            """
            INSERT INTO slugs (slug, content_hash)
            VALUES (?, ?)
            ON CONFLICT(slug) DO UPDATE SET content_hash = excluded.content_hash
            """,
            (slug, content_hash),
        )
        self._maybe_commit()

    def resolve_slug(self, slug: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content_hash FROM slugs WHERE slug = ?", (slug,)
        ).fetchone()
        return row["content_hash"] if row else None

    def list_slugs(self) -> List[Tuple[str, str]]:
        """All ``(slug, content_hash)`` pairs, ordered by slug."""
        rows = self.conn.execute(
            "SELECT slug, content_hash FROM slugs ORDER BY slug"
        ).fetchall()
        return [(r["slug"], r["content_hash"]) for r in rows]

    # --- reporting helpers --------------------------------------------------

    def count_folios(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM folios").fetchone()[0]

    def count_threads(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

    def unresolved_endpoints(self) -> List[str]:
        """Thread endpoints that are legacy ids still awaiting an alias.

        A resolved folio edge stores a ``sha256::`` content hash; an actor
        endpoint was routed to the weaver and stored as NULL. Anything left —
        a non-null endpoint that is neither a content hash nor a known alias —
        is a dangling or cross-project reference holding its legacy id, which
        resolves lazily if/when the target imports and registers an alias.
        """
        rows = self.conn.execute(
            """
            SELECT DISTINCT endpoint FROM (
                SELECT from_id AS endpoint FROM threads
                UNION
                SELECT to_id   AS endpoint FROM threads
            )
            WHERE endpoint IS NOT NULL
              AND endpoint NOT LIKE 'sha256::%'
              AND endpoint NOT IN (SELECT legacy_id FROM aliases)
            ORDER BY endpoint
            """
        ).fetchall()
        return [r["endpoint"] for r in rows]
