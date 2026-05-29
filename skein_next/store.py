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
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

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

    def close(self) -> None:
        self.conn.close()

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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()

    def resolve_alias(self, legacy_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content_hash FROM aliases WHERE legacy_id = ?", (legacy_id,)
        ).fetchone()
        return row["content_hash"] if row else None
