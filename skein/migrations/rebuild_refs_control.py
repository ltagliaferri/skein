#!/usr/bin/env python3
# SPENT — ran ecosystem-wide 2026-07-08 (41 stale rows fixed across 8 dbs), and
# the columns it rebuilds were dropped the same day by drop_refs_control.py (the
# threads-only contraction). Inapplicable to migrated/fresh dbs. Kept as a record;
# also the rebuild recipe an OLD-code rollback would need after a .bak restore.
"""Maintenance: rebuild the refs control cache from the control-thread truth.

Between the Phase 3a A3 cutover (whose step-2 rebuild corrected the cache once,
~2026-07-01) and the ``save_thread`` cache-refresh landing, the generic
POST /threads path (``skein close``) wrote genesis-keyed control threads WITHOUT
refreshing ``refs.status/assigned_to/archived`` — only the PATCH sugar path did
(via ``save_folio``). Every close taken through the generic path since the
cutover left a stale cache row (measured 2026-07-08: 14 in skein's db, 9 in
speakbot's; closed briefs listed as open on the unenriched read surfaces).

This one-shot recomputes the three cached columns for EVERY ref from the threads
table, using the identical genesis-anchored ``(created_at DESC, thread_id DESC)``
reduction the A4 readers (``_latest_control_by_folio``) and the new
``_refresh_control_cache`` use:

  - status      <- latest ``type='status'``  self-loop on the genesis (default 'open')
  - assigned_to <- latest ``type='assignment'`` from the genesis (NULL when none)
  - archived    <- 1 iff the latest ``type='archive'`` marker content == 'archived'

Unlike the A3 migration this rewrites NO thread rows — it only writes the cache
columns — so it is safe to re-run any time the cache is suspected stale, and a
run against an already-consistent db is a verified no-op (0 changed).

Properties (mirror retire_folios_fts.py):
  - Stats first. ``--dry-run`` reads only and reports how many rows would change
    per column, so the blast radius is known before any write.
  - Idempotent. A second run reports 0 changes.
  - Atomic + race-free per db. The rebuild runs inside one ``BEGIN IMMEDIATE``
    transaction; the write lock means no concurrent save interleaves.
  - Consistent backups. ``--backup`` uses SQLite's online-backup API before the
    write (``.bak-refs-control-rebuild-<stamp>`` beside each db).
  - In ``--all``, one failing db is isolated, reported, and skipped; the rest run
    and the process exits non-zero so the operator re-runs the skipped ones.
  - Rollback needs no mapping: the cache is fully DERIVED from threads, so a
    re-run (or the .bak) reconstructs it.

Usage:
    # one db — what would change (read-only), then apply with a backup
    python -m skein.migrations.rebuild_refs_control --dry-run PATH/skein.db
    python -m skein.migrations.rebuild_refs_control --backup PATH/skein.db

    # every registered project — blast radius (read-only), then apply
    python -m skein.migrations.rebuild_refs_control --all --dry-run
    python -m skein.migrations.rebuild_refs_control --all --backup
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from skein.storage import load_project_registry

BUSY_TIMEOUT_MS = 30000

# Per cached column: the thread-derived expression (the SAME reduction the
# readers use, as a correlated subquery on refs.genesis_hash) and the current
# cached expression it must equal. UPDATE and stale-COUNT are both composed from
# the derived != cached predicate, so "what would change", "what changed", and
# "what is left" are one definition. IS NOT handles NULLs (assigned_to).
_DERIVED = {
    "status": """COALESCE(
        (SELECT t.content FROM threads t
         WHERE t.type = 'status' AND t.to_id = refs.genesis_hash
         ORDER BY t.created_at DESC, t.thread_id DESC LIMIT 1),
        'open')""",
    "assigned_to": """
        (SELECT t.to_id FROM threads t
         WHERE t.type = 'assignment' AND t.from_id = refs.genesis_hash
         ORDER BY t.created_at DESC, t.thread_id DESC LIMIT 1)""",
    "archived": """CASE WHEN
        (SELECT t.content FROM threads t
         WHERE t.type = 'archive' AND t.to_id = refs.genesis_hash
         ORDER BY t.created_at DESC, t.thread_id DESC LIMIT 1)
        = 'archived' THEN 1 ELSE 0 END""",
}

_CACHED = {
    "status": "COALESCE(status, 'open')",
    "assigned_to": "assigned_to",
    "archived": "COALESCE(archived, 0)",
}

_COLUMNS = tuple(_DERIVED)


def _stale_counts(conn: sqlite3.Connection) -> dict:
    """How many rows per column differ from the thread-derived value (read-only:
    the exact predicate the rebuild UPDATEs on, counted instead of updated)."""
    out = {}
    for name in _COLUMNS:
        out[name] = conn.execute(
            f"SELECT COUNT(*) FROM refs "
            f"WHERE {_CACHED[name]} IS NOT ({_DERIVED[name]})"
        ).fetchone()[0]
    return out


def rebuild_db(db_path: Path, *, dry_run: bool) -> dict:
    """Rebuild the three cache columns of one db. Returns per-column counts:
    rows that differed (dry-run) / rows changed (apply, asserted equal to the
    pre-count inside the same write transaction)."""
    # isolation_level=None -> autocommit; we drive BEGIN IMMEDIATE / COMMIT.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        stats = {"refs": conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]}
        if dry_run:
            stats.update(_stale_counts(conn))
            return stats

        conn.execute("BEGIN IMMEDIATE")  # write lock BEFORE reading the counts
        try:
            expected = _stale_counts(conn)
            for name in _COLUMNS:
                cur = conn.execute(
                    f"UPDATE refs SET {name} = ({_DERIVED[name]}) "
                    f"WHERE {_CACHED[name]} IS NOT ({_DERIVED[name]})"
                )
                changed = cur.rowcount
                if changed != expected[name]:
                    raise RuntimeError(
                        f"{name}: changed {changed} rows, expected "
                        f"{expected[name]} (concurrent write inside the lock?)"
                    )
                stats[name] = changed
            leftover = _stale_counts(conn)
            if any(leftover.values()):
                raise RuntimeError(f"rebuild left stale rows: {leftover}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return stats
    finally:
        conn.close()


def _backup_db(db_path: Path) -> Path:
    """A consistent single-file backup via SQLite's online-backup API."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.bak-refs-control-rebuild-{stamp}")
    src = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        dst = sqlite3.connect(str(backup))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return backup


def _print_stats(label: str, stats: dict, dry_run: bool) -> None:
    diffs = {k: v for k, v in stats.items() if k != "refs" and v}
    if not diffs:
        print(f"  {label}: clean | refs {stats['refs']}")
        return
    verb = "would fix" if dry_run else "fixed"
    detail = ", ".join(f"{k}={v}" for k, v in diffs.items())
    print(f"  {label}: {verb} {detail} | refs {stats['refs']}")


def _db_paths_from_registry() -> list:
    out = []
    for pid, entry in load_project_registry().items():
        if not isinstance(entry, dict):
            print(f"  skipping {pid}: malformed registry entry")
            continue
        data_dir = entry.get("data_dir")
        base = entry.get("path")
        if data_dir:
            db = Path(data_dir) / "skein.db"
        elif base:
            db = Path(base) / ".skein" / "data" / "skein.db"
        else:
            print(f"  skipping {pid}: registry entry has no path/data_dir")
            continue
        if db.exists():
            out.append((pid, db))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild the refs control cache (status/assigned_to/archived) "
        "from control threads."
    )
    ap.add_argument("db", nargs="?", help="path to one skein.db")
    ap.add_argument("--all", action="store_true",
                    help="run on every registered project's db")
    ap.add_argument("--dry-run", action="store_true",
                    help="read-only: report what would change")
    ap.add_argument("--backup", action="store_true",
                    help="online-backup each db before writing")
    args = ap.parse_args()

    if bool(args.db) == bool(args.all):
        ap.error("give exactly one of: a db path, or --all")

    targets = (
        _db_paths_from_registry() if args.all
        else [(Path(args.db).name, Path(args.db))]
    )
    if not targets:
        print("no dbs found")
        return 1

    failed = []
    for label, db_path in targets:
        try:
            if not args.dry_run and args.backup:
                print(f"  {label}: backup -> {_backup_db(db_path).name}")
            stats = rebuild_db(db_path, dry_run=args.dry_run)
            _print_stats(label, stats, args.dry_run)
        except Exception as e:
            failed.append(label)
            print(f"  {label}: FAILED — {e}")

    if failed:
        print(f"\n{len(failed)} db(s) failed: {', '.join(failed)} — re-run those")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
