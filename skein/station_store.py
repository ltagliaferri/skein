"""StationStore — the federation-station role surface over the working skein store.

Station re-home Stage 1 (Fork B). A *station* holds signed, multi-author content
received on the wire; a *workbench* holds locally-authored lineages. Under Fork B they
share ONE folio object store — the working ``versions`` table — plus the post-swap
``threads`` table and the station sidecar tables (``station_slugs``, ``aliases``, the
federation set). ``LogDatabase(station=True)`` births that schema (Stage 1a); this module
is the station's *accessor surface* over it.

Why a separate class rather than methods on ``LogDatabase``:
  * Four skein_next method names the station servers call (``get_folio``, ``save_thread``,
    ``get_threads``, ``search_folios``) already exist on ``LogDatabase`` with incompatible
    ``Folio``-object signatures — they cannot be re-defined on the same class.
  * The servers depend on ONE long-lived connection with the skein_next posture
    (rollback-journal so the read surface can mount the corpus ``:ro``; ``busy_timeout`` +
    ``foreign_keys`` on; ``transaction()``/``savepoint()``). ``LogDatabase`` opens a fresh
    WAL connection per call — the wrong posture for a station.
So ``StationStore`` presents the EXACT skein_next store interface, refs-free, over the
shared tables. Later stages re-point the ingress/read servers at it in place of
``SkeinNextStore`` with minimal rewiring.

**Strict-null narrowing (decided with Patrick, 2026-07-09).** skein_next's ``folios`` and
``threads`` are all-nullable; the shared ``versions``/``threads`` keep the workbench's
``NOT NULL`` on the structural canonical columns. The station therefore REQUIRES those
fields non-null (folio: type/title/content/created_at/created_by; thread:
from_id/to_id/type/created_at — weaver and content stay nullable) and raises
``ValueError`` on a null one. This is a deliberate, tested narrowing vs skein_next: safe
because the only in-scope producer (a workbench publisher) reads its own ``NOT NULL``
columns onto the wire, so a conforming publish never carries the forbidden nulls.

Exception surface: the null guard raises ``ValueError``; a NON-null but wrong-typed field
(e.g. an int title, a non-datetime ``created_at``) raises canon's own ``CanonError`` /
``TypeError`` from the hash step that runs first. This never breaks the "never 500s"
posture because the re-homed ingress runs the total ``wire.*_reject_reason`` gate before
this store AND wraps the write in ``except Exception -> "invalid fields"`` — it catches
ANY exception, not only ``ValueError``. Callers must not narrow that catch to
``ValueError`` alone.

This module covers Stage 1b (folio/thread/alias accessors) and 1c (``latest_statuses`` +
the genesis-anchored ``station_slugs`` derived-head resolver). Federation-table accessors
ride with their servers in later stages.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import quote

from .identity import compute_folio_hash, compute_thread_hash, normalize_created_at
from .storage import LogDatabase
from .utils import generate_thread_id

# The station corpus filename under a data directory. skein_next used ``store.db``; the
# re-homed station runs on skein, so it is ``skein.db`` (revisited at Stage 6 config).
DB_FILENAME = "skein.db"

# How long a write waits for a held lock before giving up (SQLite default 0 = fail
# instantly). Ported from skein_next: lets concurrent ingress writers serialize under a
# rollback-journal store instead of getting an instant 'database is locked'.
BUSY_TIMEOUT_MS = 5000


# ── search helpers (ported verbatim from skein_next/store.py — the station search is the
# skein_next L1 substring rank, NOT the workbench FTS5 path) ─────────────────────────────

def _like_escape(s: str) -> str:
    """Escape a literal string for use inside a ``LIKE ... ESCAPE '\\'`` pattern.

    The backslash is escaped first so the later ``%``/``_`` escapes are not doubled.
    Callers add their own ``%`` wildcards around the result.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Relevance weights: a query term in the title outweighs one in the body.
_TITLE_WEIGHT = 3
_BODY_WEIGHT = 1

# Cap on distinct query terms — ``?q=`` is unauthenticated on a mesh-facing surface; an
# unbounded term list drives O(terms x candidates x content) substring scans and can trip
# the SQLite variable limit. 32 is far more than any real query.
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


# The six folio columns the station reads out of ``versions`` — the exact skein_next
# folio-dict contract (same names as skein_next ``folios``). Selected explicitly so the
# dict is those six keys regardless of any future ``versions`` column.
_FOLIO_COLS = "content_hash, type, created_at, created_by, title, content"


def _existing_db_role(path) -> Optional[str]:
    """Classify an EXISTING db at ``path`` without writing it: ``"station"`` (has the
    ``station_slugs`` table — the station-only marker), ``"other"`` (a db with tables but
    NO ``station_slugs``, i.e. a workbench corpus — even one migrated to a ``thread_hash``
    PK by ``threads_pk_swap``), or ``None`` (absent / empty / unreadable → birthing a
    station here is safe). Opens read-only so a path is vetted BEFORE any DDL runs.

    The discriminator is ``station_slugs``, not the ``threads`` PK shape: a workbench db
    that has undergone the ``threads_pk_swap`` migration carries the SAME ``thread_hash``
    PK a station does, so PK shape can't tell them apart — but only a station ever has
    ``station_slugs``.
    """
    if not Path(path).exists():
        return None
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not tables:
            return None
        if "station_slugs" not in tables:
            return "other"
        cols = {r[1] for r in conn.execute("PRAGMA table_info(station_slugs)")}
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    # A station_slugs table present but WITHOUT the genesis-anchored shape is a corrupt or
    # spoofed marker (skein_next's flat slugs had slug+content_hash, not anchor_hash):
    # classify non-station so birth REFUSES rather than mutating the db.
    return "station" if {"slug", "anchor_hash"} <= cols else "other"


class StationStore:
    """The skein_next store interface, refs-free, over the shared skein station tables.

    NOT safe for concurrent use of a SINGLE instance: ``_in_batch``/``_sp_counter`` and the
    one connection are shared unlocked state, so ``check_same_thread=False`` is for handing
    one instance to one request that uses it SERIALLY (the skein_next posture: one store per
    request), never for concurrent calls on the same instance."""

    # --- lifecycle ----------------------------------------------------------

    def __init__(
        self,
        data_dir: Optional[Any] = None,
        *,
        db_path: Optional[Any] = None,
        check_same_thread: bool = True,
        read_only: bool = False,
    ):
        """Open the station store.

        Pass ``data_dir`` (the corpus lives at ``data_dir/skein.db``) or an explicit
        ``db_path``. ``read_only`` opens the corpus without ever writing it — no mkdir, no
        DDL, no commit (``mode=ro``/``immutable=1``) — for the read surface; the store
        must already exist. ``check_same_thread=False`` lets a threadpool request touch a
        connection created on another thread (the request uses it serially).
        """
        if db_path is not None:
            self.db_path = Path(db_path)
        elif data_dir is not None:
            self.db_path = Path(data_dir) / DB_FILENAME
        else:
            raise ValueError("StationStore requires data_dir or db_path")
        self.read_only = read_only
        if read_only:
            self.conn = self._connect_read_only(check_same_thread)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            # Refuse an existing NON-station corpus BEFORE any DDL runs. A station uses the
            # SAME default filename (data_dir/skein.db) LogDatabase does; without this
            # pre-birth check, LogDatabase(station=True) below would bolt the station
            # sidecar tables onto a workbench db irreversibly. Checking read-only, before
            # birth, means a mismatched db is never altered. (A concurrent workbench
            # creation of this path in the check→birth window is a TOCTOU deferred to the
            # Stage-6 config toggle that binds role→data-dir; no Stage-1 caller races it.)
            if _existing_db_role(self.db_path) == "other":
                raise ValueError(
                    f"StationStore refuses an existing non-station db at {self.db_path} "
                    f"(no well-formed station_slugs table — a workbench corpus, or a "
                    f"corrupt/incompletely-birthed one?)"
                )
            # Birth/ensure the schema via the SINGLE DDL owner (LogDatabase, station role).
            # A station db is born rollback-journal (LogDatabase._get_connection is
            # station-aware on EVERY connection), so this connection and the served corpus
            # stay rollback-journal with no WAL↔rollback flip.
            LogDatabase(self.db_path, station=True)
            self.conn = sqlite3.connect(str(self.db_path), check_same_thread=check_same_thread)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            # Enforced from the first write (the constituent_attribution FK); MUST be set
            # outside any transaction, before writes.
            self.conn.execute("PRAGMA foreign_keys=ON")
        # Confirm the opened corpus is a station (the read_only path does no pre-birth
        # check). Close the just-opened connection if we bail so a retry loop over a
        # misconfigured path doesn't leak a handle per attempt.
        try:
            self._assert_station_corpus()
        except Exception:
            self.conn.close()
            raise
        self._in_batch = False
        self._sp_counter = 0

    def _assert_station_corpus(self) -> None:
        """The opened corpus must be a station: it has the ``station_slugs`` marker table
        (a workbench never does, migrated or not) AND a POST-swap ``threads`` table
        (``thread_hash`` PK — the content-address dedup ``save_thread`` relies on)."""
        tables = {
            r["name"] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        cols = (
            {r["name"] for r in self.conn.execute("PRAGMA table_info(station_slugs)")}
            if "station_slugs" in tables
            else set()
        )
        if not {"slug", "anchor_hash"} <= cols:
            raise ValueError(
                f"{self.db_path} is not a station corpus (no well-formed station_slugs "
                f"table — a workbench db?)"
            )
        pk = [r["name"] for r in self.conn.execute("PRAGMA table_info(threads)") if r["pk"]]
        if pk != ["thread_hash"]:
            raise ValueError(
                f"{self.db_path} threads is not post-swap (PK {pk or 'none'}, "
                f"not thread_hash) — dedup would be broken"
            )

    def _connect_read_only(self, check_same_thread: bool) -> sqlite3.Connection:
        """Open the corpus read-only, never writing it. Tries ``mode=ro`` (WAL-aware)
        then ``immutable=1`` (a read-only filesystem where SQLite can't create sidecars)."""
        # read_only requires an EXISTING corpus and must never create one. The immutable=1
        # fallback URI carries no mode= param, so on a missing path SQLite would open it
        # read-write-CREATE and leave a 0-byte skein.db behind — violating the no-write
        # contract. Refuse a missing path up front instead.
        if not self.db_path.exists():
            raise sqlite3.OperationalError(
                f"read-only station corpus does not exist: {self.db_path}"
            )
        # Percent-encode the path into the file: URI (keeping '/' as the separator): a raw
        # path with '#' or '?' (ticket numbers, versioned dir names) otherwise starts a URI
        # fragment/query and SQLite silently opens a truncated, wrong path.
        p = quote(str(self.db_path), safe="/")
        last_err: Optional[Exception] = None
        # Both URIs carry mode=ro so neither can CREATE a file: bare immutable=1 defaults to
        # read-write-create and would leave a 0-byte db if the path vanished mid-open.
        for uri in (f"file:{p}?mode=ro", f"file:{p}?immutable=1&mode=ro"):
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread)
                conn.row_factory = sqlite3.Row
                conn.execute("SELECT 1 FROM versions LIMIT 1")  # resolve the open
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

    def __enter__(self) -> "StationStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- transactions -------------------------------------------------------

    def _maybe_commit(self) -> None:
        """Commit immediately unless inside a batch transaction."""
        if not self._in_batch:
            self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator["StationStore"]:
        """Batch many writes into a single commit (ported from skein_next).

        ``BEGIN IMMEDIATE`` grabs the write lock up front so concurrent writers queue on
        it and ``busy_timeout`` serializes them (a DEFERRED txn would deadlock on the
        read→write upgrade and fail instantly). The BEGIN runs before ``_in_batch`` is
        set, so a failed lock leaves the store clean. Not re-entrant.
        """
        if self._in_batch:
            raise RuntimeError("transaction() is not re-entrant")
        self.conn.execute("BEGIN IMMEDIATE")
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
        """A nested SAVEPOINT for per-item isolation inside a batch (ported from
        skein_next). A block that raises rolls back only its writes, then re-raises — so
        the ingress can reject one item and let its siblings commit. Re-entrant.

        MUST be nested inside :meth:`transaction`. Standalone, a write's own
        ``_maybe_commit`` would COMMIT (releasing every open savepoint) inside the block,
        so the rollback would then fail with 'no such savepoint' AND the write would be
        permanently committed — the opposite of isolation. Guarded so misuse fails loudly
        rather than silently committing a rejected item."""
        if not self._in_batch:
            raise RuntimeError("savepoint() must be used inside transaction()")
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

    # --- folios (over the shared ``versions`` table, refs-free) -------------

    def create_folio(self, fields: Mapping[str, Any]) -> str:
        """Normalize, hash, and idempotently insert a folio into ``versions``. Returns
        its content hash. The caller does NOT pass ``content_hash`` — it is recomputed.

        Strict-null narrowing: the station requires the structural canonical fields
        (type/title/content/created_at/created_by) non-null; a null one raises
        ``ValueError`` (the ingress reports it as 'invalid fields'). See the module
        docstring.
        """
        content_hash = compute_folio_hash(fields)
        normalized_created_at = normalize_created_at(fields.get("created_at"))
        row = {
            "type": fields.get("type"),
            "title": fields.get("title"),
            "content": fields.get("content"),
            "created_at": normalized_created_at,
            "created_by": fields.get("created_by"),
        }
        missing = [k for k, v in row.items() if v is None]
        if missing:
            raise ValueError(
                "station folio requires non-null " + ", ".join(sorted(missing))
                + f" (content_hash={content_hash})"
            )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO versions
                (content_hash, type, created_at, created_by, title, content)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                content_hash,
                row["type"],
                row["created_at"],
                row["created_by"],
                row["title"],
                row["content"],
            ),
        )
        self._maybe_commit()
        return content_hash

    def get_folio(self, content_hash: str) -> Optional[Dict[str, Any]]:
        """Read one folio by content hash — the 6-col dict, or ``None`` (never ``{}``)."""
        row = self.conn.execute(
            f"SELECT {_FOLIO_COLS} FROM versions WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return dict(row) if row else None

    def list_folios(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"""
            SELECT {_FOLIO_COLS} FROM versions
            ORDER BY created_at, content_hash
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_folios(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Folios matching ``query``, AND-of-terms, ranked title-over-body (skein_next L1).

        A folio matches only if EVERY whitespace-split term appears (case-insensitively)
        in its title or content; terms are matched literally (``LIKE`` wildcards escaped).
        A bounded recency-ordered candidate window is pulled, then ranked in Python — NOT
        a whole-corpus rank — so the tiebreak and which rows survive match skein_next.
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
        params.append(max(limit * 5, 200))
        rows = self.conn.execute(
            f"""
            SELECT {_FOLIO_COLS} FROM versions
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

        ``prefix`` is matched literally against the full ``sha256::<hex>`` address
        (``LIKE`` metacharacters escaped). Returns up to ``limit`` bare hash strings so a
        caller can detect an ambiguous prefix.
        """
        like = _like_escape(prefix) + "%"
        rows = self.conn.execute(
            """
            SELECT content_hash FROM versions
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
        """Member folios of a site, joined through ``within`` threads, ordered by the
        folio's ``(created_at, content_hash)``. ``DISTINCT`` guards against a folio
        holding two membership edges to the same site."""
        sql = [
            f"SELECT DISTINCT {', '.join('f.' + c for c in _FOLIO_COLS.split(', '))}",
            "FROM versions f",
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

    def count_folios(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"]

    def latest_statuses(self, folio_hashes: List[str]) -> Dict[str, str]:
        """Map each given folio hash to its latest status-thread content (1c control).

        Status is thread-derived: a ``type=status`` thread points ``to_id`` at the folio;
        the latest by ``(created_at, thread_hash)`` wins. Keyed by folio HASH, refs-free —
        NOT the workbench's refs-slug-keyed ``_latest_control_by_folio`` (option (b)). A
        folio with no status thread is simply absent (the method NEVER invents 'open';
        callers do ``.get(h, 'open')``). The IN-list is chunked at 900 to stay under the
        SQLite variable limit; an empty input yields ``{}``.
        """
        out: Dict[str, str] = {}
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
            # Ascending order → the last row written for each folio is the newest;
            # thread_hash breaks equal-created_at ties deterministically.
            for r in rows:
                out[r["to_id"]] = r["content"]
        return out

    # --- threads (over the shared post-swap ``threads`` table, refs-free) ---

    def save_thread(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
        weaver: Optional[str] = None,
        created_at: Any = None,
        content: Optional[str] = None,
    ) -> str:
        """Idempotently store a thread edge keyed on its content hash. Returns the hash.

        Refs-free — unlike the workbench ``save_thread``, the station stores wire
        endpoints verbatim (no ``_genesis_key_control``/``genesis_of_slug``). Dedup is the
        ``INSERT OR IGNORE`` on the post-swap ``thread_hash`` PK. A ``thread_id`` is
        generated to satisfy the audit column — it is NOT part of the content hash, so it
        never affects dedup. Strict-null: from_id/to_id/type/created_at must be non-null.
        """
        thread_hash = compute_thread_hash(from_id, to_id, type, weaver, created_at, content)
        normalized_created_at = normalize_created_at(created_at)
        required = {
            "from_id": from_id,
            "to_id": to_id,
            "type": type,
            "created_at": normalized_created_at,
        }
        missing = [k for k, v in required.items() if v is None]
        if missing:
            raise ValueError(
                "station thread requires non-null " + ", ".join(sorted(missing))
                + f" (thread_hash={thread_hash})"
            )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO threads
                (thread_hash, thread_id, from_id, to_id, type, weaver, created_at, content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_hash,
                generate_thread_id(),
                from_id,
                to_id,
                type,
                weaver,
                normalized_created_at,
                content,
            ),
        )
        self._maybe_commit()
        return thread_hash

    def get_thread(self, thread_hash: str) -> Optional[Dict[str, Any]]:
        """Read one thread by its content hash (symmetric with get_folio). The dict
        carries the extra harmless ``thread_id`` audit key alongside the 7 wire columns."""
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
        """Query threads by any AND-combination of from_id/to_id/type, ordered
        ``(created_at, thread_hash)`` ASC (envelope consumes this order for dedup + BFS)."""
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

    # --- aliases (flat legacy-id -> content_hash) ---------------------------

    def resolve_alias(self, legacy_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content_hash FROM aliases WHERE legacy_id = ?", (legacy_id,)
        ).fetchone()
        return row["content_hash"] if row else None

    # --- slugs / naming (1c — genesis-anchored, derived head) ---------------
    #
    # A station slug is a CLAIM ``(slug, anchor_hash = the lineage GENESIS content hash,
    # claimed_by, scope)``; resolution DERIVES the head by walking ``supersedes`` edges
    # forward from the anchor over versions the station holds — never a stored mutable
    # head, never ``refs`` (Risk-3). The skein_next resolve_slug/list_slugs CONTRACT
    # (slug→hash / [(slug,hash)]) is preserved; the mechanism is the derived walk. Site
    # slugs are the degenerate case: the site folio is its own genesis, no walk. Wire
    # folio-slug claims + ingress admission arrive in a later stage; Stage 1 exposes
    # ``set_slug`` (last-write-wins, as the ingress uses it for site folios).

    def set_slug(self, slug: str, content_hash: str) -> None:
        """Bind ``slug`` to a lineage anchored at ``content_hash`` (upsert, LAST-WRITE-
        WINS). ``content_hash`` is the anchor (genesis) of the claim; for a site folio
        (the only Stage-1 caller, via ingress) the site is its own genesis, so the anchor
        is the site hash and resolution returns it directly."""
        self.conn.execute(
            """
            INSERT INTO station_slugs (slug, anchor_hash, claimed_by, scope)
            VALUES (?, ?, NULL, NULL)
            ON CONFLICT(slug) DO UPDATE SET anchor_hash = excluded.anchor_hash
            """,
            (slug, content_hash),
        )
        self._maybe_commit()

    def _version_exists(self, content_hash: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM versions WHERE content_hash = ? LIMIT 1", (content_hash,)
            ).fetchone()
            is not None
        )

    def _derive_heads(self, anchor_hash: str) -> List[str]:
        """The head(s) of a lineage: walk ``supersedes`` forward from ``anchor_hash`` over
        versions the station holds. A ``supersedes`` edge is ``(from_id=new, to_id=old)``,
        so a version's successor is the ``from_id`` of a ``supersedes`` thread whose
        ``to_id`` is that version. A version with no HELD successor is a head (iff the
        station holds it). Usually one head; a fork (two supersedes children) yields two —
        resolution surfaces the fork, never a silent winner. Terminates on cycles via the
        seen-set (a well-formed graph should be acyclic; the guard is defensive).

        This resolver does NOT verify signatures — it reduces over whatever ``supersedes``
        edges the station HOLDS. Signature/admission (only signed supersedes edges enter
        the store) is the ingress/verify stage's responsibility, upstream of here; a
        forged edge must be rejected at admission, not detected in resolution."""
        heads: List[str] = []
        seen: set = set()
        stack = [anchor_hash]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            successors = [
                r["from_id"]
                for r in self.conn.execute(
                    "SELECT from_id FROM threads WHERE type = 'supersedes' AND to_id = ?",
                    (node,),
                )
            ]
            held = [s for s in successors if self._version_exists(s)]
            if not held:
                if self._version_exists(node):
                    heads.append(node)
            else:
                stack.extend(held)
        return sorted(set(heads))

    def resolve_slug_heads(self, slug: str) -> List[str]:
        """All held heads a slug resolves to: ``[]`` if unclaimed, one for a normal
        lineage, more than one for a fork (surfaced, never silently collapsed)."""
        row = self.conn.execute(
            "SELECT anchor_hash FROM station_slugs WHERE slug = ?", (slug,)
        ).fetchone()
        if not row:
            return []
        return self._derive_heads(row["anchor_hash"])

    def resolve_slug(self, slug: str) -> Optional[str]:
        """The single head ``slug`` names, or ``None`` when unclaimed OR forked. A fork
        never returns one of its branches (no silent winner); callers wanting the fork use
        :meth:`resolve_slug_heads`. Preserves the skein_next slug→hash|None contract for
        the degenerate (site / un-forked) case."""
        heads = self.resolve_slug_heads(slug)
        return heads[0] if len(heads) == 1 else None

    def list_slugs(self) -> List[Tuple[str, str]]:
        """All ``(slug, head)`` pairs ordered by slug — the un-forked, resolvable ones
        (a forked or unresolvable claim is omitted rather than named to a silent winner)."""
        out: List[Tuple[str, str]] = []
        for row in self.conn.execute(
            "SELECT slug, anchor_hash FROM station_slugs ORDER BY slug"
        ).fetchall():
            heads = self._derive_heads(row["anchor_hash"])
            if len(heads) == 1:
                out.append((row["slug"], heads[0]))
        return out

    def folio_site_slug(self, content_hash: str) -> Optional[str]:
        """The site slug of a member folio (alphabetically-first if it is in several).

        Joins the folio's ``within`` edge to a ``station_slugs`` claim on the site's
        anchor (``anchor_hash = within.to_id`` — the degenerate site case, the site being
        its own genesis)."""
        row = self.conn.execute(
            """
            SELECT s.slug AS slug
            FROM threads t
            JOIN station_slugs s ON s.anchor_hash = t.to_id
            WHERE t.type = 'within' AND t.from_id = ?
            ORDER BY s.slug
            LIMIT 1
            """,
            (content_hash,),
        ).fetchone()
        return row["slug"] if row else None

    def folio_site_slugs(self, content_hashes: Optional[List[str]] = None) -> Dict[str, str]:
        """Map member folio hashes to their site slugs (alphabetically-first on multi-site
        via ``setdefault`` over ``ORDER BY slug``). ``None`` maps the whole corpus in one
        join; a hash list is chunked at 900 (for labelling a bounded result set)."""
        mapping: Dict[str, str] = {}
        if content_hashes is None:
            rows = self.conn.execute(
                """
                SELECT t.from_id AS folio, s.slug AS slug
                FROM threads t
                JOIN station_slugs s ON s.anchor_hash = t.to_id
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
                JOIN station_slugs s ON s.anchor_hash = t.to_id
                WHERE t.type = 'within' AND t.from_id IN ({placeholders})
                ORDER BY s.slug
                """,
                chunk,
            ).fetchall()
            for r in rows:
                mapping.setdefault(r["folio"], r["slug"])
        return mapping
