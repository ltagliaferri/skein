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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from .identity import compute_folio_hash, compute_thread_hash, normalize_created_at


def _now_iso() -> str:
    """A timezone-aware UTC isoformat stamp for first-insert timestamps."""
    return datetime.now(timezone.utc).isoformat()

DEFAULT_DATA_DIR = Path(".skein-next")
DB_FILENAME = "store.db"


def _like_escape(s: str) -> str:
    """Escape a literal string for use inside a ``LIKE ... ESCAPE '\\'`` pattern.

    The backslash is escaped first so the later ``%``/``_`` escapes are not
    doubled. Callers add their own ``%`` wildcards around the result.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Relevance weights for search ranking (L1): a query term found in the title is
# worth more than one found in the body.
_TITLE_WEIGHT = 3
_BODY_WEIGHT = 1

# Cap on distinct query terms. `?q=` is unauthenticated on a mesh-facing surface;
# an unbounded term list drives O(terms x candidates x content) substring scans
# and, on SQLite < 3.32 (999-variable limit), a >499-term query would raise. 32
# terms is far more than any real query and bounds both the work and the binds.
_MAX_SEARCH_TERMS = 32


def _search_score(row: Mapping[str, Any], terms: List[str]) -> int:
    """Relevance of a folio for the search terms: title hits weigh over body hits."""
    title = (row.get("title") or "").lower()
    content = (row.get("content") or "").lower()
    score = 0
    for term in terms:
        t = term.lower()
        if t in title:
            score += _TITLE_WEIGHT
        if t in content:
            score += _BODY_WEIGHT
    return score


def make_snippet(content: Optional[str], terms: List[str], width: int = 180) -> Optional[str]:
    """A short excerpt of ``content`` around the first matched term (legibility).

    Untrusted display text — it goes inside the agent-markdown fence and is HTML-
    escaped in the web view, never trusted. Returns ``None`` for empty content.
    """
    if not content:
        return None
    flat = " ".join(content.split())  # collapse whitespace for a clean one-liner
    low = flat.lower()
    positions = [low.find(t.lower()) for t in terms]
    hits = [p for p in positions if p >= 0]
    if not hits:
        head = flat[:width]
        return head + ("…" if len(flat) > width else "")
    start = max(0, min(hits) - width // 3)
    end = min(len(flat), start + width)
    snippet = flat[start:end]
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(flat) else "")


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

-- Signature overlay (DEPRECATED — superseded by the manifest model below; the
-- per-folio signature path is removed with the envelope folio_verdict rewrite,
-- Thread D). Kept transiently so the read path stays green during the migration.
CREATE TABLE IF NOT EXISTS folio_signatures (
    content_hash TEXT PRIMARY KEY,
    bundle_json  TEXT NOT NULL
);

-- Manifest store (the unified signing model). One row per (manifest, signer): a
-- publish signs ONE Merkle-root descriptor over EVERY constituent's content hash
-- (folios AND threads). The (root, issuer, subject) TRIPLE PK lets two distinct
-- bound signers each retain a proof over the identical constituent set (ST3 / the
-- rev-5 B50 property); a bare-root PK would have shadowed the second.
CREATE TABLE IF NOT EXISTS manifests (
    root            TEXT,          -- the Merkle root ("sha256::<hex>"), signed anchor
    manifest_hash   TEXT,          -- content address of the signed descriptor (lookup key)
    descriptor_json TEXT NOT NULL, -- the canonical descriptor (root + leaf_count)
    leaf_list_json  TEXT NOT NULL, -- the full sorted, deduped constituent hash list
    bundle_json     TEXT NOT NULL, -- the single Sigstore bundle over the descriptor
    issuer          TEXT,          -- verified manifest signer
    subject         TEXT,
    leaf_count      INTEGER,
    created_at      TEXT,          -- IMMUTABLE first-insert timestamp
    PRIMARY KEY (root, issuer, subject)
);

-- Unified constituent attribution: one row per covered constituent (a folio
-- content_hash OR a thread_hash), pointing at its FIRST covering manifest
-- (first-manifest-wins, Q3). The FK references the full proof triple, so the
-- identity a constituent denormalizes is exactly the proof it resolves to
-- (proof-signer == display-signer, ST6).
CREATE TABLE IF NOT EXISTS constituent_attribution (
    constituent_hash TEXT PRIMARY KEY, -- a folio content_hash OR a thread_hash
    kind             TEXT NOT NULL,    -- 'folio' | 'thread' (triage/display)
    root             TEXT NOT NULL,
    issuer           TEXT NOT NULL,    -- denormalized manifest signer (display-authoritative)
    subject          TEXT NOT NULL,
    created_at       TEXT,
    FOREIGN KEY (root, issuer, subject) REFERENCES manifests(root, issuer, subject)
);

-- Account bindings (the per-instance authorization sidecar). A (issuer, subject)
-- is authorized as 'operator' or 'author'; revoked_at NULL = active. created_at
-- is set ONCE and preserved across revoke/reactivate (B5). The single-active-
-- operator invariant is LOGIC-enforced (bootstrap refuses a second; rotate revokes
-- the old in the same transaction), not a DB constraint — get_operator() resolves
-- deterministically even under a corrupt two-operator state (B47).
CREATE TABLE IF NOT EXISTS account_bindings (
    issuer            TEXT,
    subject           TEXT,
    role              TEXT,   -- 'operator' | 'author'
    vouched_by_issuer  TEXT,
    vouched_by_subject TEXT,
    created_at        TEXT,
    revoked_at        TEXT,   -- NULL = active
    PRIMARY KEY (issuer, subject)
);

-- Append-only binding audit: every lifecycle transition writes one event row in
-- the same transaction as the mutation. Rows are INSERTed, never UPDATEd/DELETEd.
CREATE TABLE IF NOT EXISTS binding_events (
    event_seq          INTEGER PRIMARY KEY,  -- rowid; monotonic insertion order
    issuer             TEXT,
    subject            TEXT,
    event              TEXT,  -- created|revoked|reactivated|rotated_in|rotated_out|promoted
    role               TEXT,
    vouched_by_issuer  TEXT,
    vouched_by_subject TEXT,
    at                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_manifests_hash ON manifests(manifest_hash);

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
            # foreign_keys MUST be enabled at CONNECTION OPEN, OUTSIDE any
            # transaction (inside one it is a silent no-op), and BEFORE the schema
            # DDL — so the constituent_attribution FK is enforced from the first
            # write (ST7). A dormant-FK store would pass FK-dependent cells by mere
            # absence-of-raise.
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        self._in_batch = False
        self._sp_counter = 0

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

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """A nested SAVEPOINT for per-item isolation inside a batch transaction.

        A block that raises rolls back ONLY this savepoint (its writes), then
        re-raises — so the ingress can catch the exception, record a reject for
        that one item, and let its siblings commit (RS13). Cheap and re-entrant
        (each call gets a unique savepoint name)."""
        self._sp_counter += 1
        name = f"sp_{self._sp_counter}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            raise
        else:
            self.conn.execute(f"RELEASE {name}")

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
        """Folios matching ``query``, AND-of-terms, ranked title-over-body (L1).

        The query is split on whitespace into terms; a folio matches only if EVERY
        term appears (case-insensitively) in its title or content — so a
        multi-word search is an AND of its words, not one exact-phrase substring
        (brief-20260522-4yt4 Layer 1). Results are relevance-ranked in Python: a
        term in the title weighs more than one in the body, recency breaks ties.
        Each term is matched literally; SQL ``LIKE`` wildcards are escaped, so
        ``50%`` or ``a_b`` find those strings. (Not FTS5/BM25 — the new store has
        no full-text index; this is the modest L1 polish over substring matching.)
        """
        terms = [t for t in query.split() if t][:_MAX_SEARCH_TERMS]
        if not terms:
            return []
        clause = " AND ".join(
            ["(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')"] * len(terms)
        )
        params: List[Any] = []
        for term in terms:
            like = "%" + _like_escape(term) + "%"
            params.extend([like, like])
        # Pull a generous recency-ordered candidate set, then rank in Python. The
        # candidate cap keeps ranking bounded; recency ordering becomes the tie
        # break (a stable sort preserves it within equal relevance scores).
        params.append(max(limit * 5, 200))
        rows = self.conn.execute(
            f"""
            SELECT * FROM folios
            WHERE {clause}
            ORDER BY created_at DESC, content_hash
            LIMIT ?
            """,
            params,
        ).fetchall()
        ranked = sorted(
            (dict(r) for r in rows),
            key=lambda r: _search_score(r, terms),
            reverse=True,
        )
        return ranked[:limit]

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

    # --- manifest store + constituent attribution (the unified signing model) --

    def add_manifest(
        self,
        root: str,
        manifest_hash: str,
        descriptor_json: str,
        leaf_list_json: str,
        bundle_json: str,
        issuer: str,
        subject: str,
        leaf_count: int,
    ) -> None:
        """Record a manifest proof. INSERT OR IGNORE on the (root, issuer, subject)
        triple, so the same signer re-publishing the same set is idempotent and
        ``created_at`` is preserved (ST2/ST9); two distinct signers over one set
        each retain their proof (ST3)."""
        self.conn.execute(
            """
            INSERT OR IGNORE INTO manifests
                (root, manifest_hash, descriptor_json, leaf_list_json, bundle_json,
                 issuer, subject, leaf_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root,
                manifest_hash,
                descriptor_json,
                leaf_list_json,
                bundle_json,
                issuer,
                subject,
                leaf_count,
                _now_iso(),
            ),
        )
        self._maybe_commit()

    def get_manifest_proof(
        self, root: str, issuer: str, subject: str
    ) -> Optional[Dict[str, Any]]:
        """The single manifest proof row for a (root, issuer, subject) triple."""
        row = self.conn.execute(
            "SELECT * FROM manifests WHERE root = ? AND issuer = ? AND subject = ?",
            (root, issuer, subject),
        ).fetchone()
        return dict(row) if row else None

    def get_manifest_proofs_by_root(self, root: str) -> List[Dict[str, Any]]:
        """The SET of proof rows for one constituent set (every signer of ``root``)."""
        rows = self.conn.execute(
            "SELECT * FROM manifests WHERE root = ? ORDER BY created_at, subject",
            (root,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_manifest_by_hash(self, manifest_hash: str) -> Optional[Dict[str, Any]]:
        """A manifest proof looked up by its content address (the indexed key)."""
        row = self.conn.execute(
            "SELECT * FROM manifests WHERE manifest_hash = ? LIMIT 1",
            (manifest_hash,),
        ).fetchone()
        return dict(row) if row else None

    def add_constituent_attribution(
        self,
        constituent_hash: str,
        kind: str,
        root: str,
        issuer: str,
        subject: str,
    ) -> None:
        """Attribute a covered constituent to its FIRST covering manifest.

        INSERT OR IGNORE on the constituent hash (first-manifest-wins, Q3/ST4); a
        later covering manifest persists as a proof row in ``manifests`` (ST3) but
        does not re-point the constituent. With foreign_keys ON the manifest parent
        must already exist (ST8) — the ingress writes the manifest row first in the
        same savepoint."""
        self.conn.execute(
            """
            INSERT OR IGNORE INTO constituent_attribution
                (constituent_hash, kind, root, issuer, subject, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (constituent_hash, kind, root, issuer, subject, _now_iso()),
        )
        self._maybe_commit()

    def get_constituent_proof(self, constituent_hash: str) -> Optional[Dict[str, Any]]:
        """Resolve a constituent to its covering manifest proof, or ``None``.

        Joins constituent_attribution -> manifests on the proof triple. If the
        manifest parent is ABSENT (a corrupted / mid-migration / FK-off dangling
        state) the row still resolves to a ``proof_missing`` sentinel carrying the
        denormalized (issuer, subject) — display-authoritative attribution degrades
        gracefully, never a 500 (ST5)."""
        attr = self.conn.execute(
            "SELECT * FROM constituent_attribution WHERE constituent_hash = ?",
            (constituent_hash,),
        ).fetchone()
        if attr is None:
            return None
        manifest = self.conn.execute(
            "SELECT * FROM manifests WHERE root = ? AND issuer = ? AND subject = ?",
            (attr["root"], attr["issuer"], attr["subject"]),
        ).fetchone()
        out: Dict[str, Any] = {
            "constituent_hash": attr["constituent_hash"],
            "kind": attr["kind"],
            "root": attr["root"],
            "issuer": attr["issuer"],
            "subject": attr["subject"],
            "created_at": attr["created_at"],
        }
        if manifest is None:
            out["proof_missing"] = True
            return out
        out["proof_missing"] = False
        out["manifest_hash"] = manifest["manifest_hash"]
        out["descriptor_json"] = manifest["descriptor_json"]
        out["leaf_list_json"] = manifest["leaf_list_json"]
        out["bundle_json"] = manifest["bundle_json"]
        out["leaf_count"] = manifest["leaf_count"]
        return out

    # --- account bindings + audit (the authorization sidecar) ----------------

    def _binding_from_row(self, row) -> "authorization.Binding":
        from . import authorization
        return authorization.Binding(
            issuer=row["issuer"],
            subject=row["subject"],
            role=row["role"],
            vouched_by_issuer=row["vouched_by_issuer"],
            vouched_by_subject=row["vouched_by_subject"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
        )

    def _log_binding_event(
        self, issuer, subject, event, role, vouched_by_issuer, vouched_by_subject
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO binding_events
                (issuer, subject, event, role, vouched_by_issuer, vouched_by_subject, at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (issuer, subject, event, role, vouched_by_issuer, vouched_by_subject, _now_iso()),
        )

    def get_binding(self, issuer: str, subject: str):
        """The binding for a (issuer, subject) pair as a ``Binding``, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM account_bindings WHERE issuer = ? AND subject = ?",
            (issuer, subject),
        ).fetchone()
        return self._binding_from_row(row) if row else None

    def add_binding(
        self,
        issuer: str,
        subject: str,
        role: str = "author",
        vouched_by_issuer: Optional[str] = None,
        vouched_by_subject: Optional[str] = None,
        event: Optional[str] = None,
    ):
        """Add (or reactivate) a binding; returns the resulting ``Binding``.

        A fresh pair INSERTs with a 'created' event. A revoked pair REACTIVATES the
        SAME row — revoked_at back to NULL, created_at PRESERVED (B5) — with a
        'reactivated' event. An already-active pair is idempotent: one row,
        created_at unchanged, NO event (B6). ``event`` overrides the audit verb
        (e.g. 'rotated_in' during a rotation)."""
        existing = self.conn.execute(
            "SELECT * FROM account_bindings WHERE issuer = ? AND subject = ?",
            (issuer, subject),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO account_bindings
                    (issuer, subject, role, vouched_by_issuer, vouched_by_subject,
                     created_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (issuer, subject, role, vouched_by_issuer, vouched_by_subject, _now_iso()),
            )
            self._log_binding_event(
                issuer, subject, event or "created", role,
                vouched_by_issuer, vouched_by_subject,
            )
        elif existing["revoked_at"] is not None:
            # reactivate the same row; created_at preserved (B5)
            self.conn.execute(
                """
                UPDATE account_bindings
                   SET revoked_at = NULL, role = ?,
                       vouched_by_issuer = ?, vouched_by_subject = ?
                 WHERE issuer = ? AND subject = ?
                """,
                (role, vouched_by_issuer, vouched_by_subject, issuer, subject),
            )
            self._log_binding_event(
                issuer, subject, event or "reactivated", role,
                vouched_by_issuer, vouched_by_subject,
            )
        # else: already active -> idempotent no-op, no event (B6)
        self._maybe_commit()
        return self.get_binding(issuer, subject)

    def revoke_binding(self, issuer: str, subject: str, event: Optional[str] = None) -> bool:
        """Revoke an ACTIVE binding (sets revoked_at; the row stays, B3). Returns
        False if there is no active row to revoke (absent or already revoked, B4)
        — no exception, no row created. ``event`` overrides the audit verb."""
        cur = self.conn.execute(
            """
            UPDATE account_bindings SET revoked_at = ?
             WHERE issuer = ? AND subject = ? AND revoked_at IS NULL
            """,
            (_now_iso(), issuer, subject),
        )
        if cur.rowcount == 0:
            self._maybe_commit()
            return False
        existing = self.get_binding(issuer, subject)
        self._log_binding_event(
            issuer, subject, event or "revoked", existing.role,
            existing.vouched_by_issuer, existing.vouched_by_subject,
        )
        self._maybe_commit()
        return True

    def promote_to_operator(self, issuer: str, subject: str):
        """Promote an EXISTING author row to operator, preserving created_at (D19).

        If the row is revoked it is reactivated and promoted (created_at preserved
        by the same UPDATE). Logs a 'promoted' event."""
        self.conn.execute(
            """
            UPDATE account_bindings SET role = 'operator', revoked_at = NULL
             WHERE issuer = ? AND subject = ?
            """,
            (issuer, subject),
        )
        b = self.get_binding(issuer, subject)
        self._log_binding_event(
            issuer, subject, "promoted", "operator",
            b.vouched_by_issuer, b.vouched_by_subject,
        )
        self._maybe_commit()
        return b

    def get_operator(self):
        """The active operator, resolved DETERMINISTICALLY (ORDER BY created_at
        LIMIT 1) — single-valued even under a corrupt two-operator state (B47).
        ``None`` when no operator is active (B9)."""
        row = self.conn.execute(
            """
            SELECT * FROM account_bindings
             WHERE role = 'operator' AND revoked_at IS NULL
             ORDER BY created_at, issuer, subject LIMIT 1
            """
        ).fetchone()
        return self._binding_from_row(row) if row else None

    def count_active_operators(self) -> int:
        """Active-operator count — the startup invariant refuses boot on != 1 (D13/D20)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM account_bindings WHERE role = 'operator' AND revoked_at IS NULL"
        ).fetchone()[0]

    def list_active_bindings(self) -> List[Any]:
        """All active bindings as ``Binding`` objects, ordered by role then subject."""
        rows = self.conn.execute(
            """
            SELECT * FROM account_bindings WHERE revoked_at IS NULL
             ORDER BY role, issuer, subject
            """
        ).fetchall()
        return [self._binding_from_row(r) for r in rows]

    def list_bindings(self, include_revoked: bool = False) -> List[Any]:
        """All bindings (active, plus revoked when ``include_revoked``)."""
        sql = "SELECT * FROM account_bindings"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY role, issuer, subject"
        return [self._binding_from_row(r) for r in self.conn.execute(sql).fetchall()]

    def get_binding_events(
        self, issuer: Optional[str] = None, subject: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """The append-only audit trail, in insertion order (optionally filtered)."""
        clauses, params = [], []
        if issuer is not None:
            clauses.append("issuer = ?")
            params.append(issuer)
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        sql = "SELECT * FROM binding_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY event_seq"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

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
