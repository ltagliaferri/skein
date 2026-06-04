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


def _like_escape(s: str) -> str:
    """Escape a literal string for use inside a ``LIKE ... ESCAPE '\\'`` pattern.

    The backslash is escaped first so the later ``%``/``_`` escapes are not
    doubled. Callers add their own ``%`` wildcards around the result.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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

-- Signature overlay (kept OUT of the folios table so identity stays the five
-- canonical fields and nothing else — same reason status/assignment are threads,
-- not columns). A folio's signature_bundle is data about one folio, not a
-- relationship, so it is a sidecar keyed by content hash rather than a thread.
CREATE TABLE IF NOT EXISTS folio_signatures (
    content_hash TEXT PRIMARY KEY,
    bundle_json  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_from ON threads(from_id);
CREATE INDEX IF NOT EXISTS idx_threads_to   ON threads(to_id);
CREATE INDEX IF NOT EXISTS idx_threads_type ON threads(type);
CREATE INDEX IF NOT EXISTS idx_folios_created_at ON folios(created_at);
"""


class SkeinNextStore:
    """Content-hash-native folio/thread/alias store backed by SQLite.

    **Single-threaded per instance (not internally synchronized).** A store wraps
    one SQLite connection and a bare ``_in_batch`` transaction flag with no lock,
    so a single ``SkeinNextStore`` MUST NOT be shared across threads — concurrent
    use could interleave ``transaction()`` toggles and commit one caller's writes
    outside another's transaction (zr29 MEDIUM #7). The supported concurrency model
    is a connection (and store) per thread/request: the web surface opens one store
    per request (``check_same_thread=False``, used serially within that request),
    and the CLI/bridge are single-threaded. Do not hand one instance to a pool.
    """

    def __init__(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        check_same_thread: bool = True,
        read_only: bool = False,
    ):
        """Open the store under ``data_dir`` (creating it unless ``read_only``).

        ``check_same_thread`` defaults to SQLite's safe single-thread mode, which
        suits the import bridge and CLI. The web server (Slice 4) opens one store
        per request and may touch its connection from a different threadpool
        thread than the one that created it, so it passes ``check_same_thread=
        False``. That is safe here because a request uses its connection serially
        (no concurrent access to the same connection); a separate connection per
        request keeps writers/readers isolated.

        ``read_only`` opens the store without ever writing it — no ``mkdir``, no
        schema DDL, no commit — for serving a corpus on a read-only mount (the
        web read surface / containerized instance). Without it the normal open
        path runs ``CREATE ... IF NOT EXISTS`` on connect, which only happens to
        succeed on a read-only mount by luck (no-op DDL on an already-complete,
        non-WAL store) and would 500 the moment the schema drifts or the store is
        WAL-mode. The store must already exist when ``read_only`` is set.
        """
        self.data_dir = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
        self.db_path = self.data_dir / DB_FILENAME
        self.read_only = read_only
        if read_only:
            self.conn = self._connect_read_only(check_same_thread)
        else:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.db_path), check_same_thread=check_same_thread)
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        self._in_batch = False

    def _connect_read_only(self, check_same_thread: bool) -> sqlite3.Connection:
        """Open the store read-only, never writing the corpus. Tries ``mode=ro``
        (WAL-aware) then ``immutable=1`` (a read-only filesystem where SQLite
        can't create its sidecar files), mirroring ``bridge.open_legacy``."""
        p = str(self.db_path)
        last_err: Optional[Exception] = None
        for uri in (f"file:{p}?mode=ro", f"file:{p}?immutable=1"):
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread)
                conn.row_factory = sqlite3.Row
                conn.execute("SELECT 1 FROM folios LIMIT 1")  # resolve the open
                return conn
            except sqlite3.OperationalError as e:
                last_err = e
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        raise sqlite3.OperationalError(
            f"could not open {p} read-only (mode=ro or immutable=1): {last_err}"
        )

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
        like = "%" + _like_escape(query) + "%"
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
        like = _like_escape(prefix) + "%"
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

    def folios_in_site(
        self,
        site_hash: str,
        type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Member folios of a site, joined through ``within`` threads.

        One SQL join (``threads.from_id = folios.content_hash`` where the
        ``within`` edge points ``to_id = site_hash``) ordered by the *folio's*
        ``created_at`` then hash, so ``limit`` returns a stable earliest-N window
        — not the arbitrary slice that limiting the unordered edge list would
        give (``within`` edges carry no timestamp, so they have no meaningful
        order of their own). ``DISTINCT`` guards against a folio somehow holding
        two membership edges to the same site.
        """
        sql = [
            "SELECT DISTINCT f.* FROM folios f",
            "JOIN threads t ON t.from_id = f.content_hash",
            "WHERE t.to_id = ? AND t.type = 'within'",
        ]
        params: List[Any] = [site_hash]
        if type is not None:
            sql.append("AND f.type = ?")
            params.append(type)
        sql.append("ORDER BY f.created_at, f.content_hash")
        if limit is not None:
            sql.append("LIMIT ?")
            params.append(limit)
        rows = self.conn.execute("\n".join(sql), params).fetchall()
        return [dict(r) for r in rows]

    def folio_site_slug(self, content_hash: str) -> Optional[str]:
        """The site slug of a single member folio (alphabetically-first if many).

        Indexed single-row lookup via ``idx_threads_from`` — for the folio-detail
        hot path, where building the whole corpus map (``folio_site_slugs``) just
        to read one entry is a full scan that grows with the corpus.
        """
        row = self.conn.execute(
            """
            SELECT s.slug AS slug
            FROM threads t
            JOIN slugs s ON s.content_hash = t.to_id
            WHERE t.type = 'within' AND t.from_id = ?
            ORDER BY s.slug
            LIMIT 1
            """,
            (content_hash,),
        ).fetchone()
        return row["slug"] if row else None

    def folio_site_slugs(self, content_hashes: Optional[List[str]] = None) -> Dict[str, str]:
        """Map member folio content hashes to their site slugs.

        With ``content_hashes`` given, the join is restricted to those folios (a
        chunked IN-list) — for labelling a bounded result set such as search hits.
        With ``None``, the whole corpus is mapped in one join (the web index, which
        labels every folio). If a folio belongs to more than one site (unusual),
        the alphabetically-first slug wins so the result is deterministic.
        """
        mapping: Dict[str, str] = {}
        if content_hashes is None:
            rows = self.conn.execute(
                """
                SELECT t.from_id AS folio, s.slug AS slug
                FROM threads t
                JOIN slugs s ON s.content_hash = t.to_id
                WHERE t.type = 'within'
                ORDER BY s.slug
                """
            ).fetchall()
            for r in rows:
                mapping.setdefault(r["folio"], r["slug"])
            return mapping
        hashes = list(content_hashes)
        for i in range(0, len(hashes), 900):
            chunk = hashes[i : i + 900]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"""
                SELECT t.from_id AS folio, s.slug AS slug
                FROM threads t
                JOIN slugs s ON s.content_hash = t.to_id
                WHERE t.type = 'within' AND t.from_id IN ({placeholders})
                ORDER BY s.slug
                """,
                chunk,
            ).fetchall()
            for r in rows:
                mapping.setdefault(r["folio"], r["slug"])
        return mapping

    def latest_statuses(self, folio_hashes: List[str]) -> Dict[str, str]:
        """Map each given folio hash to its most recent status-thread content.

        Status is thread-derived (there is no status column): a ``type=status``
        thread points ``to_id`` at the folio, and the latest one by ``created_at``
        wins. One batched query instead of one per folio. Folios with no status
        thread are simply absent from the result.
        """
        out: Dict[str, str] = {}
        # Chunk the IN-list to stay well under SQLITE_MAX_VARIABLE_NUMBER (as low
        # as 999 on older SQLite); a busy project can have far more folios.
        hashes = list(folio_hashes)
        for i in range(0, len(hashes), 900):
            chunk = hashes[i : i + 900]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"""
                SELECT to_id, content FROM threads
                WHERE type = 'status' AND to_id IN ({placeholders})
                ORDER BY created_at, thread_hash
                """,
                chunk,
            ).fetchall()
            # Ascending (created_at, thread_hash) → the last row written for each
            # folio is the newest; thread_hash breaks ties deterministically when
            # two statuses share a normalized created_at (the bridge collapses
            # whole-second legacy timestamps), so the winner can't flip per query.
            for r in rows:
                out[r["to_id"]] = r["content"]
        return out

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
        thread_hash = compute_thread_hash(from_id, to_id, type, weaver, created_at, content)
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

    def get_thread(self, thread_hash: str) -> Optional[Dict[str, Any]]:
        """Read one thread by its content hash (symmetric with get_folio)."""
        row = self.conn.execute(
            "SELECT * FROM threads WHERE thread_hash = ?", (thread_hash,)
        ).fetchone()
        return dict(row) if row else None

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
        row = self.conn.execute("SELECT content_hash FROM slugs WHERE slug = ?", (slug,)).fetchone()
        return row["content_hash"] if row else None

    def list_slugs(self) -> List[Tuple[str, str]]:
        """All ``(slug, content_hash)`` pairs, ordered by slug."""
        rows = self.conn.execute("SELECT slug, content_hash FROM slugs ORDER BY slug").fetchall()
        return [(r["slug"], r["content_hash"]) for r in rows]

    # --- signature overlay (sidecar) ----------------------------------------

    def set_signature(self, content_hash: str, bundle_json: str) -> None:
        """Store (or replace) a folio's signature bundle. Overlay, not identity."""
        self.conn.execute(
            "INSERT OR REPLACE INTO folio_signatures (content_hash, bundle_json) VALUES (?, ?)",
            (content_hash, bundle_json),
        )
        self._maybe_commit()

    def get_signature(self, content_hash: str) -> Optional[str]:
        """A folio's signature bundle JSON, or ``None`` if it is unsigned."""
        row = self.conn.execute(
            "SELECT bundle_json FROM folio_signatures WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        return row["bundle_json"] if row else None

    # --- reporting helpers --------------------------------------------------

    def count_folios(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM folios").fetchone()[0]

    def count_threads(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

    def count_aliases(self) -> int:
        """Number of legacy-id aliases. ``legacy_id`` is the PK, so this is the
        distinct-alias count the import fidelity gate reconciles against."""
        return self.conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]

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
