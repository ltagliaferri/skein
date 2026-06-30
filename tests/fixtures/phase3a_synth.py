"""Deterministic synthetic fixtures for SKEIN's Phase 3a migration.

Each fixture is its own pre-migration ``skein.db`` (the schema as it exists at
``master`` today: ``thread_id``-PK ``threads``, no ``thread_hash`` column,
``folios``/``refs``/``versions`` all present), built to exercise exactly ONE
A3 transform branch — see ``docs/PHASE_3A_TEST_DESIGN.md`` §4 and
``docs/PHASE_3_DESIGN.md`` §5.A3 for the branch list this mirrors.

Pure stdlib + sqlite3 + ``skein.identity`` — no dependency on any not-yet-written
Phase 3a code (no migration module, no expected-value reducer), and no
``skein.storage``/``skein.models`` either, so this generator does not drift when
those are refactored under A1-A5. The table DDL below is hand-copied from
``skein/storage.py``'s ``_init_db`` (threads ~502, folios ~560, versions ~637,
refs ~658) and must be kept in sync with it by hand.

No ``random()``/``now()`` anywhere: every id, timestamp, and hash input is a
fixed literal, so two runs of :func:`build_all` produce byte-identical dbs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

from skein.identity import compute_folio_hash

# ── fixed identity inputs (no randomness, no wall-clock) ────────────────────

SITE = "synth-site"
AGENT = "agent-synth"
AGENT_SETTER = "agent-setter"
AGENT_ASSIGNEE = "agent-assignee"

_BASE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ts(offset_seconds: int) -> str:
    """A fixed, deterministic ISO timestamp `offset_seconds` after a fixed epoch."""
    return (_BASE_TS + timedelta(seconds=offset_seconds)).isoformat()


# ── schema (hand-copied from skein/storage.py _init_db) ─────────────────────

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS threads (
        thread_id TEXT PRIMARY KEY,
        from_id TEXT NOT NULL,
        to_id TEXT NOT NULL,
        type TEXT NOT NULL,
        content TEXT,
        weaver TEXT,
        created_at DATETIME NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_threads_from ON threads(from_id)",
    "CREATE INDEX IF NOT EXISTS idx_threads_to ON threads(to_id)",
    "CREATE INDEX IF NOT EXISTS idx_threads_type ON threads(type)",
    "CREATE INDEX IF NOT EXISTS idx_threads_created ON threads(created_at)",
    """
    CREATE INDEX IF NOT EXISTS idx_threads_to_type_created
    ON threads(to_id, type, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_threads_from_type_created
    ON threads(from_id, type, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS folios (
        folio_id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        site_id TEXT NOT NULL,
        created_at DATETIME NOT NULL,
        created_by TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        assigned_to TEXT,
        target_agent TEXT,
        omlet TEXT,
        archived INTEGER DEFAULT 0,
        metadata JSON,
        acknowledged_at DATETIME,
        content_hash TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_folios_site ON folios(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_folios_type ON folios(type)",
    "CREATE INDEX IF NOT EXISTS idx_folios_status ON folios(status)",
    "CREATE INDEX IF NOT EXISTS idx_folios_created ON folios(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_folios_created_by ON folios(created_by)",
    "CREATE INDEX IF NOT EXISTS idx_folios_assigned ON folios(assigned_to)",
    """
    CREATE TABLE IF NOT EXISTS versions (
        content_hash TEXT PRIMARY KEY,
        type         TEXT NOT NULL,
        title        TEXT NOT NULL,
        content      TEXT NOT NULL,
        created_at   DATETIME NOT NULL,
        created_by   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS refs (
        slug            TEXT PRIMARY KEY,
        genesis_hash    TEXT NOT NULL,
        head_hash       TEXT NOT NULL,
        site_id         TEXT NOT NULL,
        status          TEXT DEFAULT 'open',
        assigned_to     TEXT,
        archived        INTEGER DEFAULT 0,
        target_agent    TEXT,
        omlet           TEXT,
        acknowledged_at DATETIME,
        metadata        JSON
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_refs_site_id ON refs(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_refs_status ON refs(status)",
    "CREATE INDEX IF NOT EXISTS idx_refs_assigned_to ON refs(assigned_to)",
    "CREATE INDEX IF NOT EXISTS idx_refs_archived ON refs(archived)",
    "CREATE INDEX IF NOT EXISTS idx_refs_head_hash ON refs(head_hash)",
    "CREATE INDEX IF NOT EXISTS idx_refs_genesis_hash ON refs(genesis_hash)",
)


def _new_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)
    return conn


# ── row-level insert helpers (raw SQL, no ORM) ───────────────────────────────


def _create_folio(
    conn: sqlite3.Connection,
    *,
    folio_id: str,
    type_: str,
    site_id: str,
    created_at: str,
    created_by: str,
    title: str,
    content: str,
    status: str = "open",
    assigned_to: Optional[str] = None,
    target_agent: Optional[str] = None,
    omlet: Optional[str] = None,
    archived: int = 0,
    metadata: Optional[str] = None,
    acknowledged_at: Optional[str] = None,
    content_hash: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO folios (folio_id, type, site_id, created_at, created_by, "
        "title, content, status, assigned_to, target_agent, omlet, archived, "
        "metadata, acknowledged_at, content_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (folio_id, type_, site_id, created_at, created_by, title, content,
         status, assigned_to, target_agent, omlet, archived, metadata,
         acknowledged_at, content_hash),
    )


def _create_version(
    conn: sqlite3.Connection,
    content_hash: str,
    type_: str,
    title: str,
    content: str,
    created_at: str,
    created_by: str,
) -> None:
    conn.execute(
        "INSERT INTO versions (content_hash, type, title, content, created_at, "
        "created_by) VALUES (?,?,?,?,?,?)",
        (content_hash, type_, title, content, created_at, created_by),
    )


def _create_ref(
    conn: sqlite3.Connection,
    slug: str,
    genesis_hash: str,
    head_hash: str,
    site_id: str,
    *,
    status: str = "open",
    assigned_to: Optional[str] = None,
    archived: int = 0,
    target_agent: Optional[str] = None,
    omlet: Optional[str] = None,
    acknowledged_at: Optional[str] = None,
    metadata: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO refs (slug, genesis_hash, head_hash, site_id, status, "
        "assigned_to, archived, target_agent, omlet, acknowledged_at, metadata) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (slug, genesis_hash, head_hash, site_id, status, assigned_to, archived,
         target_agent, omlet, acknowledged_at, metadata),
    )


def _create_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    from_id: str,
    to_id: str,
    type_: str,
    content: Optional[str],
    weaver: Optional[str],
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO threads (thread_id, from_id, to_id, type, content, weaver, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (thread_id, from_id, to_id, type_, content, weaver, created_at),
    )


def _genesis_hash(
    *, title: str, content: str, created_at: str, created_by: str = AGENT,
    type_: str = "finding",
) -> str:
    """The content hash a lineage with these five fields would have.

    Just calls :func:`compute_folio_hash` — exposed under a fixture-local name so
    callers that want a hash with NO backing rows (the genesis-orphan shapes)
    don't read as if they minted a real lineage.
    """
    return compute_folio_hash({
        "type": type_, "title": title, "content": content,
        "created_at": created_at, "created_by": created_by,
    })


def _mint_lineage(
    conn: sqlite3.Connection,
    slug: str,
    *,
    created_at: str,
    title: str,
    content: str,
    created_by: str = AGENT,
    type_: str = "finding",
    site_id: str = SITE,
) -> str:
    """Write a coherent folio + versions + refs row; return its genesis hash.

    This is the "live folio with a ref" baseline nearly every shape needs —
    the shared rows a status/assignment thread resolves against.
    """
    genesis_hash = _genesis_hash(
        title=title, content=content, created_at=created_at,
        created_by=created_by, type_=type_,
    )
    _create_folio(
        conn, folio_id=slug, type_=type_, site_id=site_id, created_at=created_at,
        created_by=created_by, title=title, content=content,
    )
    _create_version(conn, genesis_hash, type_, title, content, created_at, created_by)
    _create_ref(conn, slug, genesis_hash, genesis_hash, site_id)
    return genesis_hash


# ── one builder per named shape ──────────────────────────────────────────────


def _build_status_plain_selfloop(conn: sqlite3.Connection) -> None:
    slug = "status-plain-selfloop-folio"
    _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    _create_thread(conn, "thread-status-plain-selfloop-1", slug, slug,
                    "status", "confirmed", AGENT, _ts(1))


def _build_status_nonselfloop_agent(conn: sqlite3.Connection) -> None:
    slug = "status-nonselfloop-folio"
    _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    # weaver is None on purpose: A3 must move AGENT_SETTER (today's `from_id`)
    # into weaver on re-key, not just preserve a weaver that was already there.
    _create_thread(conn, "thread-status-nonselfloop-1", AGENT_SETTER, slug,
                    "status", "confirmed", None, _ts(1))


def _build_status_slug_orphan(conn: sqlite3.Connection) -> None:
    # No folio/version/ref row at all for this slug — it was deleted.
    slug = "status-slug-orphan-deleted-folio"
    _create_thread(conn, "thread-status-slug-orphan-1", slug, slug,
                    "status", "confirmed", AGENT, _ts(0))


def _build_status_genesis_orphan(conn: sqlite3.Connection) -> None:
    # A syntactically-valid sha256:: hash that matches no live refs.genesis_hash
    # in this db (we never mint the lineage it would belong to).
    ghost = _genesis_hash(title="ghost", content="ghost body", created_at=_ts(0))
    _create_thread(conn, "thread-status-genesis-orphan-1", ghost, ghost,
                    "status", "confirmed", AGENT, _ts(1))


def _build_status_already_genesis(conn: sqlite3.Connection) -> None:
    slug = "status-already-genesis-folio"
    genesis = _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    # Already genesis-keyed AND it matches a live ref (an A2-window write) — A3
    # must pass this through untouched, never re-resolve it as a slug.
    _create_thread(conn, "thread-status-already-genesis-1", genesis, genesis,
                    "status", "confirmed", AGENT, _ts(1))


def _build_status_overlap_agent_deleted(conn: sqlite3.Connection) -> None:
    # Both non-self-loop (from=agent) AND orphan (to=deleted slug) at once —
    # the overlap A3 must resolve by dropping, never normalizing to from=NULL.
    slug = "status-overlap-deleted-folio"
    _create_thread(conn, "thread-status-overlap-1", AGENT_SETTER, slug,
                    "status", "confirmed", None, _ts(0))


def _build_status_tie_group(conn: sqlite3.Connection) -> None:
    slug = "status-tie-folio"
    _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    tie_ts = _ts(10)
    # Same folio, same created_at, different content, different thread_id — the
    # deterministic (created_at, thread_id) tiebreak is what the reader/rebuild
    # must agree on; this fixture only states the shape, not the winner.
    _create_thread(conn, "thread-status-tie-aaa", slug, slug, "status", "open",
                    AGENT, tie_ts)
    _create_thread(conn, "thread-status-tie-bbb", slug, slug, "status",
                    "confirmed", AGENT, tie_ts)


def _build_thread_byte_duplicate(conn: sqlite3.Connection) -> None:
    slug = "byte-dup-folio"
    _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    dup_ts = _ts(5)
    # Identical across all six canonical fields (from, to, type, weaver,
    # created_at, content) — differ ONLY by thread_id. 3a never collapses these
    # (no UNIQUE index until 3b); this fixture just makes sure one is present.
    _create_thread(conn, "thread-byte-dup-a001", slug, slug, "status",
                    "confirmed", AGENT, dup_ts)
    _create_thread(conn, "thread-byte-dup-b002", slug, slug, "status",
                    "confirmed", AGENT, dup_ts)


def _build_status_multi_history(conn: sqlite3.Connection) -> None:
    slug = "status-multi-history-folio"
    _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    _create_thread(conn, "thread-status-multi-1", slug, slug, "status", "open",
                    AGENT, _ts(1))
    _create_thread(conn, "thread-status-multi-2", slug, slug, "status",
                    "confirmed", AGENT, _ts(2))
    _create_thread(conn, "thread-status-multi-3", slug, slug, "status",
                    "closed", AGENT, _ts(3))


def _build_status_mixed_keyspace(conn: sqlite3.Connection) -> None:
    slug = "status-mixed-keyspace-folio"
    genesis = _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    # Earlier, slug-keyed (legacy write).
    _create_thread(conn, "thread-status-mixed-1", slug, slug, "status", "open",
                    AGENT, _ts(1))
    # Later, already genesis-keyed (an A2-window write) — the union reducer must
    # pick THIS one as latest, not the slug-keyed row.
    _create_thread(conn, "thread-status-mixed-2", genesis, genesis, "status",
                    "closed", AGENT, _ts(2))


def _build_status_mixed_keyspace_slug_later(conn: sqlite3.Connection) -> None:
    slug = "status-mixed-keyspace-slug-later-folio"
    genesis = _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    # The case that DISTINGUISHES union-latest from prefer-genesis-else-slug:
    # the genesis-keyed row is EARLIER, the slug-keyed row is LATER. A correct
    # union-then-latest reducer returns the slug-keyed 'closed' (the latest);
    # a "prefer genesis, else slug" shortcut would wrongly return the stale
    # genesis-keyed 'open'. (status_mixed_keyspace has genesis later, so both
    # rules agree there — only this ordering catches the shortcut. Design §5.A1,
    # Fell-to-Fixture r1:union-reader.)
    _create_thread(conn, "thread-status-mixedsluglater-1", genesis, genesis,
                    "status", "open", AGENT, _ts(1))
    _create_thread(conn, "thread-status-mixedsluglater-2", slug, slug, "status",
                    "closed", AGENT, _ts(2))


def _build_assignment_plain(conn: sqlite3.Connection) -> None:
    slug = "assignment-plain-folio"
    _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    _create_thread(conn, "thread-assignment-plain-1", slug, AGENT_ASSIGNEE,
                    "assignment", "Assigned finding: T", AGENT, _ts(1))


def _build_assignment_orphan(conn: sqlite3.Connection) -> None:
    slug = "assignment-orphan-deleted-folio"
    _create_thread(conn, "thread-assignment-orphan-1", slug, AGENT_ASSIGNEE,
                    "assignment", "Assigned finding: T", AGENT, _ts(0))


def _build_assignment_odd_shape(conn: sqlite3.Connection) -> None:
    # Odd shape (eq68's one ecosystem-wide odd-shape assignment row): `to_id` is
    # the folio's OWN slug rather than an assignee agent id. `from_id` IS a
    # resolvable folio slug, so A3's resolve rule still re-keys `from` to
    # genesis — the rule only ever inspects `from_id` — and must leave `to_id`
    # untouched (still the slug, not re-anchored, not "fixed up"). Exercises
    # that the odd `to` neither crashes the transform nor gets silently rewritten.
    slug = "assignment-odd-shape-folio"
    _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    _create_thread(conn, "thread-assignment-odd-shape-1", slug, slug,
                    "assignment", "Assigned to self?", AGENT, _ts(1))


def _build_archive_selfloop(conn: sqlite3.Connection) -> None:
    slug = "archive-selfloop-folio"
    genesis = _mint_lineage(conn, slug, created_at=_ts(0), title="T", content="body")
    # content='archived' is the pinned ARCHIVED_MARKER contract (finding-20260630-0r3x):
    # a folio is archived iff its latest archive thread's content == 'archived'
    # (mirrors status 'closed' vs 'open'). The Leg C reducer keys on this marker,
    # so the self-loop must carry it for the fixture to reduce archived=1.
    _create_thread(conn, "thread-archive-selfloop-1", genesis, genesis,
                    "archive", "archived", AGENT, _ts(1))


def _build_i1_genesis_collision(conn: sqlite3.Connection) -> None:
    # Two DISTINCT slugs (so two distinct refs rows / lineages) sharing the SAME
    # genesis_hash — violates I1 (genesis_hash unique per lineage). No thread
    # rows are needed: I1 is checked as an A3 PRECONDITION over refs alone,
    # before any transform runs.
    shared_at = _ts(0)
    shared_hash = _genesis_hash(title="T", content="shared body", created_at=shared_at)
    _create_version(conn, shared_hash, "finding", "T", "shared body", shared_at, AGENT)
    for slug in ("i1-collision-folio-a", "i1-collision-folio-b"):
        _create_folio(conn, folio_id=slug, type_="finding", site_id=SITE,
                       created_at=shared_at, created_by=AGENT, title="T",
                       content="shared body")
        _create_ref(conn, slug, shared_hash, shared_hash, SITE)


# ── public surface ────────────────────────────────────────────────────────-─

ALL_FIXTURES: Dict[str, str] = {
    "status_plain_selfloop": "slug status self-loop (from=to=slug), folio has a ref -> A3 re-keys to genesis self-loop",
    "status_nonselfloop_agent": "from=agent, to=slug, folio has a ref -> A3 normalizes to genesis self-loop, agent preserved in weaver",
    "status_slug_orphan": "status self-loop whose slug has NO ref (deleted folio) -> A3 drops, logged as slug orphan",
    "status_genesis_orphan": "status self-loop already genesis-keyed matching NO refs.genesis_hash -> A3 drops, logged as genesis orphan",
    "status_already_genesis": "genesis-keyed status self-loop matching a live refs.genesis_hash (A2-window write) -> A3 passes through untouched",
    "status_overlap_agent_deleted": "from=agent AND to=deleted-slug (matches both non-selfloop and orphan filters) -> A3 drops, never normalizes to NULL",
    "status_tie_group": "two status rows, same folio, same created_at, different content + thread_id -> deterministic (created_at, thread_id) tiebreak",
    "thread_byte_duplicate": "two rows identical in all six canonical fields, differing only by thread_id -> would collapse under content-addressing (3b), here just present",
    "status_multi_history": "one folio with several status rows at distinct increasing timestamps (open->confirmed->closed)",
    "status_mixed_keyspace": "one folio with both a slug-keyed and a (later) genesis-keyed status row (the A2->A3 window)",
    "status_mixed_keyspace_slug_later": "mixed-keyspace folio with the slug-keyed row LATER than the genesis-keyed one -> union-latest returns the slug value, distinguishing it from prefer-genesis-else-slug",
    "assignment_plain": "from=folio-slug (has ref), to=assignee-agent -> A3 re-keys from=genesis, to untouched",
    "assignment_orphan": "assignment whose from-slug has no ref -> A3 drops-but-logged",
    "assignment_odd_shape": "assignment row whose to_id is the folio's own slug, not an assignee agent",
    "archive_selfloop": "constructed archive self-loop (type='archive', from=to=genesis) feeding refs.archived",
    "i1_genesis_collision": "two refs rows sharing one genesis_hash (violates I1) -> drives the A3 precondition refusal",
}

_BUILDERS = {
    "status_plain_selfloop": _build_status_plain_selfloop,
    "status_nonselfloop_agent": _build_status_nonselfloop_agent,
    "status_slug_orphan": _build_status_slug_orphan,
    "status_genesis_orphan": _build_status_genesis_orphan,
    "status_already_genesis": _build_status_already_genesis,
    "status_overlap_agent_deleted": _build_status_overlap_agent_deleted,
    "status_tie_group": _build_status_tie_group,
    "thread_byte_duplicate": _build_thread_byte_duplicate,
    "status_multi_history": _build_status_multi_history,
    "status_mixed_keyspace": _build_status_mixed_keyspace,
    "status_mixed_keyspace_slug_later": _build_status_mixed_keyspace_slug_later,
    "assignment_plain": _build_assignment_plain,
    "assignment_orphan": _build_assignment_orphan,
    "assignment_odd_shape": _build_assignment_odd_shape,
    "archive_selfloop": _build_archive_selfloop,
    "i1_genesis_collision": _build_i1_genesis_collision,
}

assert set(ALL_FIXTURES) == set(_BUILDERS), "ALL_FIXTURES and _BUILDERS must name the same shapes"


def build_fixture(name: str, dir: Path) -> Path:
    """Write one named shape-scenario's skein.db under `dir`; return its path."""
    if name not in _BUILDERS:
        raise KeyError(f"unknown phase3a synthetic fixture: {name!r}")
    dir.mkdir(parents=True, exist_ok=True)
    path = dir / f"{name}.db"
    conn = _new_db(path)
    try:
        _BUILDERS[name](conn)
        conn.commit()
    finally:
        conn.close()
    return path


def build_all(dir: Path) -> Dict[str, Path]:
    """Build every fixture under `dir`; return name -> path."""
    return {name: build_fixture(name, dir) for name in ALL_FIXTURES}
