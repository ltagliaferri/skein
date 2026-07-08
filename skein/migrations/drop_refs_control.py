#!/usr/bin/env python3
"""Migration: drop the refs control cache columns — threads-only control state.

The threads-only contraction (2026-07-08, Patrick + frost-0707; follows
brief-20260708-lk46 / finding-20260708-oj4m): genesis-keyed control threads are
the SOLE persistence of status/assignment, reduced at read time by
``get_latest_statuses``/``get_latest_assignments`` and overlaid on every API read
surface via ``enrich_folios_with_status``. The ``refs.status`` /
``refs.assigned_to`` cache columns this drops were HALF-maintained (only the
PATCH sugar path refreshed them; the primary ``skein close`` path did not), which
produced the measured post-A3 staleness (41 rows across 8 dbs, rebuilt once by
``rebuild_refs_control`` before this drop). ``refs.archived`` goes with them: the
folio-archived feature was removed outright — zero archive threads and zero
archived refs existed ecosystem-wide (a never-used holdover; site archival is a
separate sites.json mechanism and is untouched).

Measured cost of losing the cache (speakbot, the biggest db: 8885 refs): the
full-corpus thread reduction is ~51ms and already runs on every list/search
read; the cache bought only a sub-ms by-status aggregate that had no production
caller. See the session record for the benchmark.

The host SQLite predates ALTER TABLE ... DROP COLUMN (needs 3.35; this box runs
3.31), so the drop is the standard table-rebuild: create ``refs_new`` with the
target schema, copy the surviving columns, DROP+RENAME, recreate the surviving
indexes (site_id/head_hash/genesis_hash — the status/assigned_to/archived
indexes die with the table).

RUN ORDER AT DEPLOY (the retire_folios_fts precedent): deploy the new code and
restart skein.service FIRST (old code SELECTs the dropped columns and would
break on a migrated db; new code never reads them, so it runs fine on an
un-migrated db), THEN run this migration. Fresh dbs born from the new DDL never
have the columns and report clean.

Properties (mirror retire_folios_fts.py):
  - Stats first. ``--dry-run`` reads only and reports which dbs still carry the
    columns and their row counts.
  - Idempotent. A migrated (or fresh-DDL) db has no columns to drop → clean.
  - Atomic + race-free per db. One BEGIN IMMEDIATE transaction; the refs row
    count is asserted unchanged across the rebuild.
  - Consistent backups. ``--backup`` uses SQLite's online-backup API before the
    rebuild (``.bak-refs-control-drop-<stamp>`` beside each db).
  - In ``--all``, one failing db is isolated, reported, and skipped; the rest run
    and the process exits non-zero so the operator re-runs the skipped ones.
  - Rollback: the dropped values are DERIVED (rebuildable from control threads by
    the old code's rebuild_refs_control) — but roll back with the .bak, since the
    old code needs the columns to exist at all.

Usage:
    python -m skein.migrations.drop_refs_control --dry-run PATH/skein.db
    python -m skein.migrations.drop_refs_control --backup PATH/skein.db
    python -m skein.migrations.drop_refs_control --all --dry-run
    python -m skein.migrations.drop_refs_control --all --backup
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from skein.storage import load_project_registry

BUSY_TIMEOUT_MS = 30000

DROP_COLUMNS = ("status", "assigned_to", "archived")
KEEP_COLUMNS = ("slug", "genesis_hash", "head_hash", "site_id",
                "target_agent", "omlet", "acknowledged_at", "metadata")
KEEP_INDEX_COLUMNS = ("site_id", "head_hash", "genesis_hash")

# Target schema — MUST stay in step with the refs DDL in skein/storage.py.
_REFS_NEW_DDL = """
CREATE TABLE refs_new (
    slug            TEXT PRIMARY KEY,
    genesis_hash    TEXT NOT NULL,
    head_hash       TEXT NOT NULL,
    site_id         TEXT NOT NULL,
    target_agent    TEXT,
    omlet           TEXT,
    acknowledged_at DATETIME,
    metadata        JSON
)
"""


def _refs_columns(conn: sqlite3.Connection) -> list:
    return [r[1] for r in conn.execute("PRAGMA table_info(refs)")]


def migrate_db(db_path: Path, *, dry_run: bool) -> dict:
    """Drop the control cache columns from one db via table rebuild."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        cols = _refs_columns(conn)
        present = [c for c in DROP_COLUMNS if c in cols]
        missing_keep = [c for c in KEEP_COLUMNS if c not in cols]
        refs_count = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        stats = {"refs": refs_count, "drops": present}
        if missing_keep:
            raise RuntimeError(
                f"refs is missing expected columns {missing_keep} — schema "
                f"drifted, refusing (have: {cols})"
            )
        if dry_run or not present:
            return stats

        conn.execute("BEGIN IMMEDIATE")
        try:
            # Recount INSIDE the write lock — this runs against a live db with
            # the service up, so the pre-lock count above (fine for reporting)
            # could be stale by the time the lock lands; comparing the copy
            # against it would fail spuriously under a concurrent write
            # (deep_code_audit finding 8, TOCTOU).
            refs_count = conn.execute(
                "SELECT COUNT(*) FROM refs").fetchone()[0]
            stats["refs"] = refs_count
            conn.execute(_REFS_NEW_DDL)
            keep_list = ", ".join(KEEP_COLUMNS)
            conn.execute(
                f"INSERT INTO refs_new ({keep_list}) "
                f"SELECT {keep_list} FROM refs"
            )
            copied = conn.execute(
                "SELECT COUNT(*) FROM refs_new").fetchone()[0]
            if copied != refs_count:
                raise RuntimeError(
                    f"row count changed across rebuild "
                    f"({refs_count} -> {copied})"
                )
            conn.execute("DROP TABLE refs")
            conn.execute("ALTER TABLE refs_new RENAME TO refs")
            # The old idx_refs_* indexes died with the table; recreate the
            # survivors exactly as _init_db does.
            for col in KEEP_INDEX_COLUMNS:
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_refs_{col} ON refs({col})"
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        after = _refs_columns(conn)
        residue = [c for c in DROP_COLUMNS if c in after]
        if residue:
            raise RuntimeError(f"drop left residue columns: {residue}")
        return stats
    finally:
        conn.close()


def _backup_db(db_path: Path) -> Path:
    """A consistent single-file backup via SQLite's online-backup API."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.bak-refs-control-drop-{stamp}")
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
    if not stats["drops"]:
        print(f"  {label}: clean (no control columns) | refs {stats['refs']}")
        return
    verb = "would drop" if dry_run else "dropped"
    print(f"  {label}: {verb} {','.join(stats['drops'])} | refs {stats['refs']}")


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
        description="Drop the refs control cache columns "
        "(status/assigned_to/archived) — control state is thread-derived."
    )
    ap.add_argument("db", nargs="?", help="path to one skein.db")
    ap.add_argument("--all", action="store_true",
                    help="run on every registered project's db")
    ap.add_argument("--dry-run", action="store_true",
                    help="read-only: report which dbs still carry the columns")
    ap.add_argument("--backup", action="store_true",
                    help="online-backup each db before rebuilding")
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
            stats = migrate_db(db_path, dry_run=args.dry_run)
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
