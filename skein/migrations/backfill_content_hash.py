#!/usr/bin/env python3
"""Migration: backfill folios.content_hash to the corrected sha256:: identity.

Phase 0 of the content-addressed migration. Recomputes every folio's content
hash from its five canonical fields (type, title, content, created_at,
created_by) through the canon/identity RSP, replacing the stale compute-once
values — old ``folio:sha256:`` framing, or digests frozen before an edit. This
is an IDENTITY migration, not a cleanup: no schema change, nothing resolves by
hash, only the ``content_hash`` column moves. See docs/PHASE_0_RESEARCH.md.

Because it changes identity values, it is deliberate:
  - Stats first. ``--dry-run`` reads only (safe on a live WAL db) and counts
    reframe-only vs digest-changed vs missing, so the blast radius is known
    before any write. ``--all --dry-run`` does this across every project.
  - An old->new mapping is logged (rollback evidence; a Phase 1 alias source).
    backfill_db returns the records; main writes the file atomically (temp +
    replace) only AFTER a successful commit — a failed/rolled-back run never
    creates or truncates a mapping, and a mapping-write failure on an
    already-committed db is reported distinctly (the db is correct; re-run).
  - Idempotent. A second run reports every folio ``unchanged``.
  - Atomic + race-free per db. The whole read-compute-write happens inside one
    ``BEGIN IMMEDIATE`` transaction, so a concurrent writer cannot edit a folio
    between the read and the rewrite. The lock — not quiescence — is what
    guarantees no torn/stale write. But the backfill holds that lock across the
    whole compute, and the live server's writer has only a ~10s busy timeout, so
    on a LARGE/BUSY db (skein, speakbot) a live save_folio can hit SQLITE_BUSY
    during the window. QUIESCE those projects before applying — the lock prevents
    corruption, it does not make a live writer succeed.
  - Consistent backups. ``--backup`` uses SQLite's online-backup API, which
    snapshots the full db (including committed WAL frames) regardless of WAL
    state — no checkpoint race, no missing -wal frames.
  - FTS triggers verified identical before and after (the content_hash UPDATE
    fires the AFTER UPDATE trigger, which re-syncs identical title/content).
  - Unparseable created_at never crashes the run: that folio is counted as an
    error, logged to the mapping, and skipped.
  - In ``--all``, one failing db (e.g. a writer holding the lock past the
    timeout) is isolated: it is reported and skipped, the rest still run, and the
    process exits non-zero so the operator knows to re-run the skipped ones.

Usage:
    # one db — stats only (read-only), then apply with a backup + mapping
    python -m skein.migrations.backfill_content_hash --dry-run PATH/skein.db
    python -m skein.migrations.backfill_content_hash --backup --mapping OUT.jsonl PATH/skein.db

    # every registered project — blast radius (read-only), then apply
    python -m skein.migrations.backfill_content_hash --all --dry-run
    python -m skein.migrations.backfill_content_hash --all --backup --mapping-dir DIR
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from skein.identity import compute_folio_hash
from skein.storage import load_project_registry

BUSY_TIMEOUT_MS = 30000
CHANGE_KINDS = ("reframed", "digest_changed", "missing", "unchanged", "error")


def _hex_of(framed: Optional[str]) -> Optional[str]:
    """The bare hex digest from any framing (folio:sha256:<hex> / sha256::<hex> / sha256:<hex>)."""
    if not framed:
        return None
    return framed.rsplit(":", 1)[-1] or None


def _classify(old: Optional[str], new: str) -> str:
    """How the new hash relates to the stored one (stats only — the written value
    is always the freshly recomputed `new`, so a misclassification is cosmetic)."""
    if not old:
        return "missing"
    if old == new:
        return "unchanged"
    if _hex_of(old) == _hex_of(new):
        return "reframed"  # same digest, only framing differs
    return "digest_changed"


def _trigger_names(conn: sqlite3.Connection) -> list:
    return sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ))


def _scan(conn: sqlite3.Connection) -> Tuple[dict, List[dict], List[tuple]]:
    """Read every folio, recompute its hash, classify. Returns (stats, mapping
    records for changed/errored folios, (new_hash, folio_id) update tuples)."""
    rows = conn.execute(
        "SELECT folio_id, type, title, content, created_at, created_by, "
        "content_hash FROM folios"
    ).fetchall()

    stats = {k: 0 for k in CHANGE_KINDS}
    records: List[dict] = []
    updates: List[tuple] = []
    for r in rows:
        old = r["content_hash"]
        try:
            new = compute_folio_hash({
                "type": r["type"],
                "title": r["title"],
                "content": r["content"],
                "created_at": r["created_at"],
                "created_by": r["created_by"],
            })
        except Exception as e:  # unparseable created_at, bad scalar, etc.
            stats["error"] += 1
            records.append({"folio_id": r["folio_id"], "old": old, "new": None,
                            "change": "error", "detail": str(e)[:200]})
            continue

        kind = _classify(old, new)
        stats[kind] += 1
        if kind != "unchanged":
            records.append({"folio_id": r["folio_id"], "old": old, "new": new,
                            "change": kind})
            updates.append((new, r["folio_id"]))

    stats["total"] = len(rows)
    stats["written"] = 0
    return stats, records, updates


def backfill_db(db_path: Path, *, dry_run: bool) -> Tuple[dict, List[dict]]:
    """Recompute content_hash for every folio in one db.

    Returns (stats, records). Apply does the whole read-compute-write inside one
    BEGIN IMMEDIATE transaction, so no concurrent writer can interleave between
    the read and the rewrite. The caller writes the mapping from ``records`` only
    after this returns (i.e. only after the commit succeeded).
    """
    # isolation_level=None -> autocommit: we drive BEGIN IMMEDIATE / COMMIT
    # ourselves, so the immediate write lock is held across read+compute+write.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        if dry_run:
            stats, records, _ = _scan(conn)
            return stats, records

        conn.execute("BEGIN IMMEDIATE")  # write lock BEFORE the read
        try:
            triggers_before = _trigger_names(conn)
            stats, records, updates = _scan(conn)
            if updates:
                conn.executemany(
                    "UPDATE folios SET content_hash = ? WHERE folio_id = ?",
                    updates,
                )
            if _trigger_names(conn) != triggers_before:
                raise RuntimeError("FTS triggers changed during backfill")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        stats["written"] = len(updates)
        return stats, records
    finally:
        conn.close()


def _write_mapping(path: Path, records: List[dict]) -> None:
    """Write the JSONL mapping atomically (temp + os.replace), so a partial or
    failed write never leaves a half-written or truncated mapping in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)  # never leave a stray temp on a failed write
        raise


def _backup_db(db_path: Path) -> Path:
    """A consistent single-file backup via SQLite's online-backup API.

    Unlike checkpoint-then-copy, this captures committed WAL frames regardless of
    WAL state and never races a concurrent writer, so the backup is always a
    coherent snapshot fit for rollback.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.bak-content-hash-{stamp}")
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
    verb = "would change" if dry_run else "changed"
    changed = stats["reframed"] + stats["digest_changed"] + stats["missing"]
    print(
        f"  {label}: {stats['total']} folios | {verb} {changed} "
        f"(reframed {stats['reframed']}, digest {stats['digest_changed']}, "
        f"missing {stats['missing']}) | unchanged {stats['unchanged']} | "
        f"errors {stats['error']}"
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
        description="Backfill folios.content_hash to the corrected sha256:: identity."
    )
    p.add_argument("db_path", type=Path, nargs="?", help="A single skein.db to migrate")
    p.add_argument("--all", action="store_true", help="Every registered project")
    p.add_argument("--dry-run", action="store_true", help="Read-only; count only")
    p.add_argument("--backup", action="store_true",
                   help="Back up each db (online-backup API) before applying")
    p.add_argument("--mapping", type=Path, help="JSONL old->new mapping (single db)")
    p.add_argument("--mapping-dir", type=Path,
                   help="Directory for per-project JSONL mappings (--all)")
    args = p.parse_args()

    if bool(args.all) == bool(args.db_path):
        p.error("give exactly one of: a db_path, or --all")
    if args.mapping and args.all:
        p.error("--mapping is for a single db; use --mapping-dir with --all")
    if args.mapping_dir and not args.all:
        p.error("--mapping-dir requires --all; use --mapping for a single db")
    if args.db_path and not args.db_path.exists():
        p.error(f"{args.db_path} does not exist")

    targets = _db_paths_from_registry() if args.all else [(str(args.db_path), args.db_path)]
    if not targets:
        print("no databases found")
        return

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"[{mode}] backfill content_hash over {len(targets)} db(s)")

    grand = {k: 0 for k in (*CHANGE_KINDS, "total", "written")}
    failures = []        # the migration itself failed; db unchanged; re-run
    mapping_failures = []  # db IS migrated, only the mapping write failed
    for label, db in targets:
        try:
            if not args.dry_run and args.backup:
                backup = _backup_db(db)
                print(f"  backed up {label} -> {backup.name}")
            stats, records = backfill_db(db, dry_run=args.dry_run)
        except Exception as e:  # isolate one bad db; keep the batch going
            print(f"  FAILED {label}: {type(e).__name__}: {e}")
            failures.append(label)
            continue

        # Write the mapping only after a successful commit. A failure here means
        # the db is already migrated correctly — report it distinctly so nobody
        # restores a good backup; a re-run is an idempotent no-op that rewrites it.
        if not args.dry_run:
            map_path = (args.mapping_dir / f"{label}.jsonl") if args.all and args.mapping_dir \
                else (args.mapping if not args.all else None)
            if map_path is not None:
                try:
                    _write_mapping(map_path, records)
                except Exception as e:
                    print(f"  {label}: MIGRATED OK but mapping write FAILED "
                          f"({type(e).__name__}: {e}); db is correct, re-run to write it")
                    mapping_failures.append(label)

        _print_stats(label, stats, args.dry_run)
        for k in grand:
            grand[k] += stats.get(k, 0)

    if len(targets) > 1:
        print("  " + "-" * 60)
        _print_stats("TOTAL", grand, args.dry_run)
    if failures:
        print(f"  {len(failures)} db(s) FAILED (db unchanged, re-run): {', '.join(failures)}")
    if mapping_failures:
        print(f"  {len(mapping_failures)} db(s) migrated but mapping unwritten: "
              f"{', '.join(mapping_failures)}")
    if failures or mapping_failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
