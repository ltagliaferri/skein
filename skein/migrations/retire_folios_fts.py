#!/usr/bin/env python3
"""Migration: retire the legacy folios_fts index and its sync triggers.

Phase 3 step 0 (the contraction's safest cut, pulled ahead of the folios drop).
``folios_fts`` is an external-content FTS5 index (content=folios) that nothing
reads any more — search moved to ``versions_fts`` at the commit-C read-flip. Its
three sync triggers (folios_ai/folios_ad/folios_au) fire on every ``INSERT OR
REPLACE INTO folios`` (every edit); because ``folios.folio_id`` is a TEXT primary
key, the implicit rowid churns under REPLACE and the external-content delete
commands accumulate orphaned postings until segments go malformed (16/44 live dbs
were FTS5-malformed; see finding-20260629-hkgv). This migration drops the table
and the three triggers, removing both the dead index and the corruption source.

It is DECOUPLED from the folios drop: the folios ROW dual-write keeps running
(the status path still needs it until the slug->genesis re-anchor lands). This
only removes folios_fts + its triggers.

Ships WITH the code change that removes the same DDL from ``_init_db``
(storage.py). Run order at deploy: deploy the new code (restart the server so
``_init_db`` no longer recreates folios_fts), THEN run this migration. If the
migration runs while old code can still construct a StorageManager, ``_init_db``
would recreate what this drops.

Properties (mirror backfill_content_hash.py):
  - Stats first. ``--dry-run`` reads only (safe on a live WAL db) and reports
    which objects exist per db, so the blast radius is known before any write.
  - Idempotent. A second run finds nothing to drop and reports ``clean``.
  - Atomic + race-free per db. The drop runs inside one ``BEGIN IMMEDIATE``
    transaction; the write lock means no concurrent save_folio interleaves. The
    folios CONTENT table is never touched — the row count is asserted unchanged
    across the drop as a guard.
  - Consistent backups. ``--backup`` uses SQLite's online-backup API before the
    drop (``.bak-folios-fts-retire-<stamp>`` beside each db), capturing committed
    WAL frames with no checkpoint race.
  - In ``--all``, one failing db is isolated, reported, and skipped; the rest run
    and the process exits non-zero so the operator re-runs the skipped ones.
  - Rollback needs no mapping: folios_fts is fully DERIVED from folios, so a
    restore from the .bak (or a CREATE VIRTUAL TABLE + 'rebuild') reconstructs it.

Usage:
    # one db — what would be dropped (read-only), then apply with a backup
    python -m skein.migrations.retire_folios_fts --dry-run PATH/skein.db
    python -m skein.migrations.retire_folios_fts --backup PATH/skein.db

    # every registered project — blast radius (read-only), then apply
    python -m skein.migrations.retire_folios_fts --all --dry-run
    python -m skein.migrations.retire_folios_fts --all --backup
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from skein.storage import load_project_registry

BUSY_TIMEOUT_MS = 30000
FTS_TRIGGERS = ("folios_ai", "folios_ad", "folios_au")
FTS_TABLE = "folios_fts"


def _present(conn: sqlite3.Connection) -> dict:
    """Which retire-targets exist in this db right now."""
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE (type='trigger' AND name IN (?,?,?)) "
            "   OR (type='table'   AND name=?)",
            (*FTS_TRIGGERS, FTS_TABLE),
        )
    }
    return {
        "table": FTS_TABLE in names,
        "triggers": sorted(t for t in FTS_TRIGGERS if t in names),
    }


def _folios_count(conn: sqlite3.Connection) -> int:
    """Row count of the CONTENT table — asserted unchanged across the drop."""
    return conn.execute("SELECT COUNT(*) FROM folios").fetchone()[0]


def retire_db(db_path: Path, *, dry_run: bool) -> dict:
    """Drop folios_fts + its three triggers from one db.

    Returns a stats dict: which objects were present, which were dropped, and the
    folios row count (unchanged by design). Apply runs inside one BEGIN IMMEDIATE
    transaction so no concurrent writer interleaves.
    """
    # isolation_level=None -> autocommit; we drive BEGIN IMMEDIATE / COMMIT.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        present = _present(conn)
        stats = {
            "table_before": present["table"],
            "triggers_before": present["triggers"],
            "dropped_table": False,
            "dropped_triggers": [],
            "folios": _folios_count(conn),
        }
        if dry_run:
            return stats

        conn.execute("BEGIN IMMEDIATE")  # write lock BEFORE the drop
        try:
            folios_before = _folios_count(conn)
            # Triggers first, then the table. DROP TRIGGER/TABLE IF EXISTS makes a
            # partial-state db (some objects already gone) a clean no-op.
            for trg in FTS_TRIGGERS:
                conn.execute(f"DROP TRIGGER IF EXISTS {trg}")
            conn.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
            folios_after = _folios_count(conn)
            if folios_after != folios_before:
                raise RuntimeError(
                    f"folios row count changed during retire "
                    f"({folios_before} -> {folios_after})"
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        after = _present(conn)
        stats["dropped_table"] = present["table"] and not after["table"]
        stats["dropped_triggers"] = [
            t for t in present["triggers"] if t not in after["triggers"]
        ]
        # Post-condition: nothing left behind.
        if after["table"] or after["triggers"]:
            raise RuntimeError(
                f"retire left residue: table={after['table']} "
                f"triggers={after['triggers']}"
            )
        return stats
    finally:
        conn.close()


def _backup_db(db_path: Path) -> Path:
    """A consistent single-file backup via SQLite's online-backup API."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.bak-folios-fts-retire-{stamp}")
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
    had_table = stats["table_before"]
    had_trgs = stats["triggers_before"]
    if not had_table and not had_trgs:
        print(f"  {label}: clean (no folios_fts, no triggers) | folios {stats['folios']}")
        return
    if dry_run:
        print(
            f"  {label}: would drop "
            f"table={'folios_fts' if had_table else '-'} "
            f"triggers={','.join(had_trgs) or '-'} | folios {stats['folios']}"
        )
    else:
        print(
            f"  {label}: dropped "
            f"table={'folios_fts' if stats['dropped_table'] else '-'} "
            f"triggers={','.join(stats['dropped_triggers']) or '-'} | "
            f"folios {stats['folios']} (unchanged)"
        )


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


def main() -> None:
    p = argparse.ArgumentParser(
        description="Retire the legacy folios_fts index and its sync triggers."
    )
    p.add_argument("db_path", type=Path, nargs="?", help="A single skein.db to migrate")
    p.add_argument("--all", action="store_true", help="Every registered project")
    p.add_argument("--dry-run", action="store_true", help="Read-only; report only")
    p.add_argument("--backup", action="store_true",
                   help="Back up each db (online-backup API) before applying")
    args = p.parse_args()

    if bool(args.all) == bool(args.db_path):
        p.error("give exactly one of: a db_path, or --all")
    if args.db_path and not args.db_path.exists():
        p.error(f"{args.db_path} does not exist")

    targets = _db_paths_from_registry() if args.all else [(str(args.db_path), args.db_path)]
    if not targets:
        print("no databases found")
        return

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"[{mode}] retire folios_fts over {len(targets)} db(s)")

    n_had = 0          # dbs that had something to drop
    failures = []      # the drop failed; db unchanged; re-run
    for label, db in targets:
        try:
            if not args.dry_run and args.backup:
                backup = _backup_db(db)
                print(f"  backed up {label} -> {backup.name}")
            stats = retire_db(db, dry_run=args.dry_run)
        except Exception as e:  # isolate one bad db; keep the batch going
            print(f"  FAILED {label}: {type(e).__name__}: {e}")
            failures.append(label)
            continue
        if stats["table_before"] or stats["triggers_before"]:
            n_had += 1
        _print_stats(label, stats, args.dry_run)

    if len(targets) > 1:
        print("  " + "-" * 60)
        verb = "would retire" if args.dry_run else "retired"
        print(f"  TOTAL: {len(targets)} db(s) | {verb} folios_fts in {n_had}")
    if failures:
        print(f"  {len(failures)} db(s) FAILED (db unchanged, re-run): {', '.join(failures)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
