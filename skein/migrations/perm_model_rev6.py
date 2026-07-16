"""One-shot migration for the permission model (rev 6, brief-20260716-u73x).

Transforms a PRE-rev6 station corpus to the rev-6 shape, ATOMICALLY and IDEMPOTENTLY
(safe to re-run, and a no-op on a corpus already born in the rev-6 shape by
``_init_station_schema``):

  1. account_bindings.role : rename 'author' -> 'originator'; add the tier CHECK
     (rebuild — SQLite cannot ALTER a CHECK onto an existing column).
  2. invites.role          : same rename + CHECK (an outstanding 'author' invite would
     otherwise become unredeemable under the new wire-redeemable set).
  3. station_slugs         : replace the single claimed_by column with the
     claimed_by_issuer + claimed_by_subject PAIR (old claimed_by was always NULL).
  4. document_grants + grant_events : create if absent.
  5. supersedes repair     : QUARANTINE pre-existing DANGLING (unheld-parent) and
     MULTI-PARENT (merge) supersedes rows, so a signed chain never has a gap and the
     <=1-parent partial-unique index can be created (§4/§5.3). Quarantined rows are
     PRESERVED in quarantined_supersedes (audited, never silently dropped).
  6. idx_threads_supersedes_one_parent : create the partial-unique index (after 5).

ATOMICITY: the whole transform runs in ONE ``BEGIN IMMEDIATE`` transaction using only
``conn.execute`` (NEVER ``executescript``, whose implicit COMMIT would break the
transaction and leave a half-migrated corpus on failure). Every table rebuild
``DROP TABLE IF EXISTS <name>_new`` first, so a re-run after a crash never trips on an
orphan scratch table. Either the corpus is fully migrated or it is untouched.

RUN BEFORE SERVING (deploy ordering): ``_init_station_schema`` creates the <=1-parent
partial-unique index on every read-write open, which raises on a corpus that still
contains merges. Run this migration (which quarantines merges first) BEFORE the station
opens the corpus read-write. The live interskein.com corpus has no supersedes edges, so
the fresh index creation is a no-op there; this ordering matters only for a pre-rev6
corpus that accumulated merges.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

_ROLE_CHECK = (
    "CHECK (role IN ('operator', 'administrator', 'steward', 'originator'))"
)


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row and row[0] else ""


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _exec_all(conn: sqlite3.Connection, statements: List[str]) -> None:
    """Run each statement with ``execute`` (which respects the open BEGIN IMMEDIATE) —
    NEVER executescript, whose implicit COMMIT would break atomicity."""
    for sql in statements:
        conn.execute(sql)


def _migrate_bindings(conn: sqlite3.Connection) -> int:
    """Rename author->originator and add the CHECK (rebuild if the CHECK is absent).
    Returns the number of rows renamed."""
    renamed = conn.execute(
        "UPDATE account_bindings SET role='originator' WHERE role='author'"
    ).rowcount
    if "CHECK" in _table_sql(conn, "account_bindings").upper():
        return renamed  # already rev-6 shape
    _exec_all(conn, [
        "DROP TABLE IF EXISTS account_bindings_new",
        f"""
        CREATE TABLE account_bindings_new (
            issuer            TEXT,
            subject           TEXT,
            role              TEXT {_ROLE_CHECK},
            vouched_by_issuer  TEXT,
            vouched_by_subject TEXT,
            created_at        TEXT,
            revoked_at        TEXT,
            PRIMARY KEY (issuer, subject)
        )
        """,
        """
        INSERT INTO account_bindings_new
            SELECT issuer, subject, role, vouched_by_issuer, vouched_by_subject,
                   created_at, revoked_at FROM account_bindings
        """,
        "DROP TABLE account_bindings",
        "ALTER TABLE account_bindings_new RENAME TO account_bindings",
    ])
    return renamed


def _migrate_invites(conn: sqlite3.Connection) -> int:
    renamed = conn.execute(
        "UPDATE invites SET role='originator' WHERE role='author'"
    ).rowcount
    if "CHECK" in _table_sql(conn, "invites").upper():
        return renamed
    cols = _columns(conn, "invites")
    collist = ", ".join(cols)
    _exec_all(conn, [
        "DROP TABLE IF EXISTS invites_new",
        f"""
        CREATE TABLE invites_new (
            token_hash            TEXT PRIMARY KEY,
            role                  TEXT NOT NULL {_ROLE_CHECK},
            created_at            TEXT,
            expires_at            TEXT NOT NULL,
            used_at               TEXT,
            revoked_at            TEXT,
            vouched_by_issuer     TEXT,
            vouched_by_subject    TEXT,
            bound_issuer          TEXT,
            bound_subject         TEXT,
            redeemed_at           TEXT,
            note                  TEXT,
            failed_attempts       INTEGER NOT NULL DEFAULT 0,
            attempts_window_start TEXT
        )
        """,
        f"INSERT INTO invites_new ({collist}) SELECT {collist} FROM invites",
        "DROP TABLE invites",
        "ALTER TABLE invites_new RENAME TO invites",
    ])
    return renamed


def _migrate_slugs(conn: sqlite3.Connection) -> bool:
    """Replace claimed_by with the claimed_by_issuer/subject pair. Returns True if a
    rebuild happened."""
    cols = _columns(conn, "station_slugs")
    if "claimed_by_issuer" in cols:
        return False  # already rev-6 shape
    _exec_all(conn, [
        "DROP TABLE IF EXISTS station_slugs_new",
        """
        CREATE TABLE station_slugs_new (
            slug                 TEXT PRIMARY KEY,
            anchor_hash          TEXT NOT NULL,
            claimed_by_issuer    TEXT,
            claimed_by_subject   TEXT,
            scope                TEXT
        )
        """,
        """
        INSERT INTO station_slugs_new (slug, anchor_hash, claimed_by_issuer,
                                       claimed_by_subject, scope)
            SELECT slug, anchor_hash, NULL, NULL, scope FROM station_slugs
        """,
        "DROP TABLE station_slugs",
        "ALTER TABLE station_slugs_new RENAME TO station_slugs",
        "CREATE INDEX IF NOT EXISTS idx_station_slugs_anchor ON station_slugs(anchor_hash)",
    ])
    return True


def _create_grant_tables(conn: sqlite3.Connection) -> None:
    _exec_all(conn, [
        """
        CREATE TABLE IF NOT EXISTS document_grants (
            anchor_hash        TEXT NOT NULL,
            grantee_issuer     TEXT NOT NULL,
            grantee_subject    TEXT NOT NULL,
            kind               TEXT NOT NULL CHECK (kind IN
                ('supersede', 'site_contribute', 'site_edit')),
            vouched_by_issuer  TEXT,
            vouched_by_subject TEXT,
            created_at         TEXT,
            revoked_at         TEXT,
            PRIMARY KEY (anchor_hash, grantee_issuer, grantee_subject, kind)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_document_grants_grantee "
        "ON document_grants(grantee_issuer, grantee_subject)",
        """
        CREATE TABLE IF NOT EXISTS grant_events (
            event_seq          INTEGER PRIMARY KEY,
            grantee_issuer     TEXT,
            grantee_subject    TEXT,
            event              TEXT,
            kind               TEXT,
            anchor_hash        TEXT,
            vouched_by_issuer  TEXT,
            vouched_by_subject TEXT,
            at                 TEXT
        )
        """,
    ])


def _repair_supersedes(conn: sqlite3.Connection) -> Dict[str, int]:
    """Quarantine DANGLING (unheld-parent) and MULTI-PARENT (merge) supersedes rows so
    the <=1-parent invariant holds and its partial-unique index can be created (§4/§5.3).
    A quarantined row is copied to quarantined_supersedes (audited) then deleted from
    threads. Forks (two children sharing a to_id) are LEGITIMATE and untouched — the
    merge audit keys on from_id, never to_id."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quarantined_supersedes (
            thread_hash TEXT,
            from_id     TEXT,
            to_id       TEXT,
            created_at  TEXT,
            quarantined_reason TEXT,
            quarantined_at TEXT
        )
        """
    )
    ts = _now()
    quarantined = 0

    # SELF-EDGE: from_id == to_id. A held self-edge is NOT dangling (its to_id is held),
    # so the dangling query misses it, yet lineage_genesis_for rejects a self-edge forever
    # (and the partial-unique index would not catch a lone self-edge). Quarantine first.
    self_edges = conn.execute(
        """
        SELECT rowid, thread_hash, from_id, to_id, created_at FROM threads
         WHERE type='supersedes' AND from_id = to_id
        """
    ).fetchall()
    for r in self_edges:
        _quarantine(conn, r, "self_edge", ts)
        quarantined += 1

    # DANGLING: a NULL to_id, or a to_id that is not a held version. NOT IN is NULL-blind
    # (a single NULL content_hash in versions — SQLite permits NULL in a TEXT PRIMARY KEY
    # — makes `to_id NOT IN (...)` evaluate to NULL/false for EVERY row); AND a NULL to_id
    # on the ROW side is likewise never selected by NOT IN. Handle both explicitly (same
    # guard class as station_store.unresolved_endpoints) so quarantine stays total.
    dangling = conn.execute(
        """
        SELECT rowid, thread_hash, from_id, to_id, created_at FROM threads
         WHERE type='supersedes'
           AND (to_id IS NULL
                OR to_id NOT IN (SELECT content_hash FROM versions WHERE content_hash IS NOT NULL))
        """
    ).fetchall()
    for r in dangling:
        _quarantine(conn, r, "dangling_parent", ts)
        quarantined += 1

    # ORPHAN CHILD: a NULL from_id, or a from_id (the NEW version) that is not held. The
    # dangling sweep above only tests the PARENT (to_id), so a pre-rev6 row like
    # supersedes(<unheld child> -> <held site genesis>) survives it — and the moment that
    # child hash is later imported, _derive_heads follows the stale edge and REDIRECTS the
    # site's slug, with no authorization ever applied (the pre-plant class §5.6 closes on
    # the live path). Under the rev-6 rules such an edge could never be admitted (the
    # from-end requires a HELD owned folio), so quarantine it.
    orphan = conn.execute(
        """
        SELECT rowid, thread_hash, from_id, to_id, created_at FROM threads
         WHERE type='supersedes'
           AND (from_id IS NULL
                OR from_id NOT IN (SELECT content_hash FROM versions WHERE content_hash IS NOT NULL))
        """
    ).fetchall()
    for r in orphan:
        _quarantine(conn, r, "orphan_child", ts)
        quarantined += 1

    merge_from_ids = [
        row[0]
        for row in conn.execute(
            """
            SELECT from_id FROM threads
             WHERE type='supersedes'
               AND to_id IN (SELECT content_hash FROM versions WHERE content_hash IS NOT NULL)
             GROUP BY from_id HAVING COUNT(*) > 1
            """
        ).fetchall()
    ]
    for from_id in merge_from_ids:
        parents = conn.execute(
            """
            SELECT rowid, thread_hash, from_id, to_id, created_at FROM threads
             WHERE type='supersedes' AND from_id=?
               AND to_id IN (SELECT content_hash FROM versions WHERE content_hash IS NOT NULL)
             ORDER BY to_id
            """,
            (from_id,),
        ).fetchall()
        for r in parents[1:]:  # keep parents[0] (smallest to_id), quarantine the rest
            _quarantine(conn, r, "merge_extra_parent", ts)
            quarantined += 1

    # CYCLES: a multi-row loop (A->B plus B->A, or longer). Each node still has <=1
    # parent, so neither the merge sweep nor the partial-unique index catches it — but
    # lineage_genesis_for fail-closes on a cycle FOREVER, leaving every version in the
    # loop permanently unauthorizable and its slug unresolvable (the same
    # permanently-stuck class as an unrepaired merge, knuth F1). Break each cycle by
    # quarantining one deterministic edge (the one whose from_id sorts first).
    cycles_broken = 0
    while True:
        edges = {
            r[0]: (r[1], r[2], r[3], r[4])
            for r in conn.execute(
                "SELECT from_id, to_id, rowid, thread_hash, created_at FROM threads "
                "WHERE type='supersedes' AND from_id IS NOT NULL"
            ).fetchall()
        }  # from_id -> (to_id, rowid, thread_hash, created_at); <=1 parent per from_id
        loop = _find_cycle(edges)
        if not loop:
            break
        victim_from = sorted(loop)[0]
        to_id, rowid, thread_hash, created_at = edges[victim_from]
        _quarantine(
            conn, (rowid, thread_hash, victim_from, to_id, created_at), "cycle_edge", ts
        )
        quarantined += 1
        cycles_broken += 1

    return {
        "quarantined": quarantined,
        "self_edges": len(self_edges),
        "dangling": len(dangling),
        "orphan_children": len(orphan),
        "cycles_broken": cycles_broken,
        "merges": len(merge_from_ids),
    }


def _find_cycle(edges: Dict[str, Any]):
    """Return the node set of ONE cycle in the parent map (from_id -> (to_id, ...)), or
    None. Each from_id has at most one parent here (merges are already quarantined), so
    the graph is functional and a walk with a seen-set finds any loop.

    LINEAR (amortized): ``in_path`` is a SET (not a list scan), and ``settled`` memoizes
    nodes already proven cycle-free ACROSS starts, so each edge is walked once. The naive
    list-scan-per-start form is O(n^3) and would stall this migration for MINUTES on a
    long revision chain while holding BEGIN IMMEDIATE — and it runs on every migration,
    even when there are zero cycles."""
    settled: set = set()  # nodes whose walk terminated with no cycle
    for start in edges:
        if start in settled:
            continue
        path: List[str] = []
        in_path: set = set()
        node = start
        while node in edges and node not in settled:
            if node in in_path:
                return set(path[path.index(node):])  # exactly the loop, tail excluded
            in_path.add(node)
            path.append(node)
            node = edges[node][0]
        settled.update(in_path)  # this whole walk reached a terminal / settled node
    return None


def _quarantine(conn: sqlite3.Connection, row, reason: str, ts: str) -> None:
    """Copy a supersedes row to quarantined_supersedes, then DELETE it by ROWID.

    ``row`` is ``(rowid, thread_hash, from_id, to_id, created_at)``. Deleting by ROWID —
    never by thread_hash — is load-bearing: SQLite permits NULL in a TEXT PRIMARY KEY, so
    a corrupt row with a NULL thread_hash would match zero rows on a
    ``WHERE thread_hash=?`` delete. That would (a) leave the row LIVE while this function
    reports it quarantined (a pre-plant edge surviving a migration that claims to have
    repaired it), and (b) make the cycle loop below spin FOREVER holding BEGIN IMMEDIATE,
    because the cycle it re-queries never goes away. rowid always identifies the row."""
    conn.execute(
        """
        INSERT INTO quarantined_supersedes
            (thread_hash, from_id, to_id, created_at, quarantined_reason, quarantined_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (row[1], row[2], row[3], row[4], reason, ts),
    )
    conn.execute("DELETE FROM threads WHERE rowid=?", (row[0],))


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def migrate(db_path: Any) -> Dict[str, Any]:
    """Apply the rev-6 permission-model migration to the station db at ``db_path``,
    ATOMICALLY (one BEGIN IMMEDIATE, no executescript) and IDEMPOTENTLY (re-runnable,
    a no-op on the rev-6 shape). Returns a report of what changed. On ANY failure the
    whole transform rolls back — the corpus is left untouched."""
    # isolation_level=None: full manual transaction control, so no DBAPI-implicit
    # BEGIN/COMMIT interferes with the single BEGIN IMMEDIATE below.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    began = False
    try:
        conn.execute("PRAGMA foreign_keys=OFF")  # table rebuilds drop/recreate
        conn.execute("BEGIN IMMEDIATE")
        began = True
        report: Dict[str, Any] = {}
        report["bindings_renamed"] = _migrate_bindings(conn)
        report["invites_renamed"] = _migrate_invites(conn)
        report["slugs_rebuilt"] = _migrate_slugs(conn)
        _create_grant_tables(conn)
        report.update(_repair_supersedes(conn))
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_supersedes_one_parent "
            "ON threads(from_id) WHERE type = 'supersedes'"
        )
        conn.execute("COMMIT")
        return report
    except BaseException:
        # Only ROLLBACK if BEGIN IMMEDIATE actually opened a transaction — if the BEGIN
        # itself failed (e.g. another writer holds the lock), there is no transaction to
        # roll back and a blind ROLLBACK would raise "cannot rollback", masking the real
        # (lock) error.
        if began:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _main() -> None:
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m skein.migrations.perm_model_rev6 <station-db-path>", file=sys.stderr)
        raise SystemExit(2)
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"no such db: {path}", file=sys.stderr)
        raise SystemExit(2)
    report = migrate(path)
    print("perm_model_rev6 migration complete:")
    for k, v in report.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    _main()
