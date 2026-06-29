#!/usr/bin/env python3
"""Migration: seed versions + refs from the existing folios rows.

Commit A of Phase 1+2 (address-by-hash + slug-as-head ref + edit-as-commit).
Pure EXPANSION: every existing folio becomes one immutable ``versions`` row
(content-addressed by its five identity fields) and one ``refs`` row (the slug
as head pointer with a copy-of-column cache of the control fields). No existing
folios row is read-modified-written; ``folios`` is untouched. Nothing reads the
new tables yet (the read-flip is commit C), so this is safe to run live ahead of
the dual-write. Its rollback is "drop the new tables". See docs/PHASE_1_2_DESIGN.md §5.

Modeled exactly on ``backfill_content_hash.py`` (the Phase 0 RSP):
  - Stats first. ``--dry-run`` reads only (safe on a live WAL db) and counts how
    many folios would seed a new versions row / a new refs row vs how many are
    already present, so the blast radius is known before any write. ``--all
    --dry-run`` does this across every project.
  - Idempotent. ``INSERT OR IGNORE`` on both tables (PKs: versions.content_hash,
    refs.slug), so a second run reports every row already-present and writes
    nothing.
  - Self-verifying versions. The hash is RECOMPUTED from the five canonical
    fields (not trusted from folios.content_hash) so a versions row's PK always
    verifies against its own columns. Since Phase 0 made folios.content_hash
    correct, the recomputed hash equals the stored one for every row; any
    divergence is counted and reported as a hash_mismatch (a Phase 0 gap).
  - Atomic + race-free per db. The whole read-compute-write happens inside one
    ``BEGIN IMMEDIATE`` transaction, so a concurrent writer cannot edit a folio
    between the read and the seed. On a LARGE/BUSY db (skein, speakbot) QUIESCE
    the project first — the lock prevents corruption, it does not make a live
    writer succeed past its ~10s busy timeout.
  - Consistent backups. ``--backup`` uses SQLite's online-backup API, which
    snapshots the full db (including committed WAL frames) regardless of WAL
    state — no checkpoint race, no missing -wal frames.
  - Schema ensured via LogDatabase. The versions/refs/versions_fts DDL is created
    by constructing ``LogDatabase(db_path)`` (idempotent ``_init_db``), so there
    is ONE source of truth for the schema and the migration cannot drift from it.
  - FTS trigger set verified identical before/after the data write (the versions
    inserts fire the versions_ai trigger, which appends to versions_fts; no
    trigger is created or dropped during the write).
  - Unparseable created_at never crashes the run: that folio is counted as an
    error, logged to the mapping, and skipped.
  - In ``--all``, one failing db is isolated: reported and skipped, the rest run,
    and the process exits non-zero so the operator knows to re-run the skipped.

Usage:
    # one db — stats only (read-only), then apply with a backup + mapping
    python -m skein.migrations.backfill_versions_refs --dry-run PATH/skein.db
    python -m skein.migrations.backfill_versions_refs --backup --mapping OUT.jsonl PATH/skein.db

    # every registered project — blast radius (read-only), then apply
    python -m skein.migrations.backfill_versions_refs --all --dry-run
    python -m skein.migrations.backfill_versions_refs --all --backup --mapping-dir DIR
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

from skein.identity import compute_folio_hash
from skein.storage import LogDatabase, load_project_registry

BUSY_TIMEOUT_MS = 30000
STAT_KINDS = (
    "version_seeded",     # a new versions row written
    "version_present",    # versions row already existed (dedup or re-run)
    "ref_seeded",         # a new refs row written
    "ref_present",        # refs row already existed (re-run)
    "hash_mismatch",      # recomputed hash != stored folios.content_hash (Phase 0 gap)
    "error",              # folio skipped (e.g. unparseable created_at)
)


def _trigger_names(conn: sqlite3.Connection) -> list:
    return sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ))


def _existing(conn: sqlite3.Connection) -> Tuple[set, set]:
    """The content_hashes already in versions and the slugs already in refs.

    Tolerates the tables not existing yet (a read-only dry-run against a copy that
    has not been constructed under the new code): treat as empty, so the
    blast-radius count reports every folio as would-seed.
    """
    def _col(table: str, col: str) -> set:
        try:
            return {r[0] for r in conn.execute(f"SELECT {col} FROM {table}")}
        except sqlite3.OperationalError as e:
            # Tolerate ONLY a missing table (un-migrated copy). Re-raise anything
            # else (locked/corrupt/disk error) so the dry-run count stays honest.
            if "no such table" in str(e).lower():
                return set()
            raise
    return _col("versions", "content_hash"), _col("refs", "slug")


def _scan(conn: sqlite3.Connection) -> Tuple[dict, List[dict], List[tuple], List[tuple]]:
    """Read every folio, recompute its hash, and build the version + ref seed rows.

    Returns (stats, mapping records, version-insert tuples, ref-insert tuples).
    A versions/refs row that already exists is counted as *present* and NOT
    re-inserted, so the returned tuples are exactly the new rows (INSERT OR IGNORE
    makes a stray re-insert harmless, but skipping keeps the written-count honest).
    """
    have_versions, have_refs = _existing(conn)
    rows = conn.execute(
        "SELECT folio_id, type, site_id, created_at, created_by, title, content, "
        "status, assigned_to, target_agent, omlet, archived, metadata, "
        "acknowledged_at, content_hash FROM folios"
    ).fetchall()

    stats = {k: 0 for k in STAT_KINDS}
    records: List[dict] = []
    version_rows: List[tuple] = []
    ref_rows: List[tuple] = []

    for r in rows:
        try:
            new_hash = compute_folio_hash({
                "type": r["type"],
                "title": r["title"],
                "content": r["content"],
                "created_at": r["created_at"],
                "created_by": r["created_by"],
            })
        except Exception as e:  # unparseable created_at, bad scalar, etc.
            stats["error"] += 1
            records.append({"folio_id": r["folio_id"], "hash": None,
                            "change": "error", "detail": str(e)[:200]})
            continue

        if r["content_hash"] and r["content_hash"] != new_hash:
            stats["hash_mismatch"] += 1
            records.append({"folio_id": r["folio_id"], "stored": r["content_hash"],
                            "recomputed": new_hash, "change": "hash_mismatch"})

        # versions row — identity only (the five hashed fields).
        if new_hash in have_versions:
            stats["version_present"] += 1
        else:
            have_versions.add(new_hash)
            stats["version_seeded"] += 1
            version_rows.append((
                new_hash, r["type"], r["title"], r["content"],
                r["created_at"], r["created_by"],
            ))

        # refs row — slug as head pointer + copy-of-column control cache.
        if r["folio_id"] in have_refs:
            stats["ref_present"] += 1
        else:
            have_refs.add(r["folio_id"])
            stats["ref_seeded"] += 1
            ref_rows.append((
                r["folio_id"],        # slug
                new_hash,             # genesis_hash
                new_hash,             # head_hash
                r["site_id"],
                r["status"] or "open",
                r["assigned_to"],
                1 if r["archived"] else 0,
                r["target_agent"],
                r["omlet"],
                r["acknowledged_at"],
                r["metadata"],  # verbatim copy-of-column (NULL stays NULL)
            ))
            records.append({"folio_id": r["folio_id"], "hash": new_hash,
                            "change": "seeded"})

    stats["total"] = len(rows)
    stats["versions_written"] = 0
    stats["refs_written"] = 0
    return stats, records, version_rows, ref_rows


def backfill_db(db_path: Path, *, dry_run: bool) -> Tuple[dict, List[dict]]:
    """Seed versions + refs for one db.

    Returns (stats, records). Apply first ensures the versions/refs/versions_fts
    schema via LogDatabase(db_path) (idempotent _init_db, the one source of truth
    for the schema), then does the whole read-compute-write inside one
    BEGIN IMMEDIATE transaction so no concurrent writer can interleave between the
    read and the seed. DRY RUN never constructs LogDatabase and never opens a
    writable connection — it is strictly read-only (safe on a live WAL db and on
    an un-migrated copy whose new tables do not exist yet; _existing tolerates
    that), matching the Phase 0 contract.
    """
    if dry_run:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            stats, records, _, _ = _scan(conn)
            return stats, records
        finally:
            conn.close()

    # Ensure the new schema exists (and matches _init_db exactly). Idempotent.
    LogDatabase(db_path)

    # isolation_level=None -> autocommit: we drive BEGIN IMMEDIATE / COMMIT
    # ourselves, so the immediate write lock is held across read+compute+write.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        conn.execute("BEGIN IMMEDIATE")  # write lock BEFORE the read
        try:
            triggers_before = _trigger_names(conn)
            stats, records, version_rows, ref_rows = _scan(conn)
            if version_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO versions "
                    "(content_hash, type, title, content, created_at, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    version_rows,
                )
            if ref_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO refs "
                    "(slug, genesis_hash, head_hash, site_id, status, assigned_to, "
                    " archived, target_agent, omlet, acknowledged_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ref_rows,
                )
            if _trigger_names(conn) != triggers_before:
                raise RuntimeError("FTS trigger set changed during backfill")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        stats["versions_written"] = len(version_rows)
        stats["refs_written"] = len(ref_rows)
        return stats, records
    finally:
        conn.close()


def _write_mapping(path: Path, records: List[dict]) -> None:
    """Write the JSONL mapping atomically (temp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _backup_db(db_path: Path) -> Path:
    """A consistent single-file backup via SQLite's online-backup API."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.bak-versions-refs-{stamp}")
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
    verb = "would seed" if dry_run else "seeded"
    print(
        f"  {label}: {stats['total']} folios | {verb} "
        f"versions {stats['version_seeded']} (present {stats['version_present']}), "
        f"refs {stats['ref_seeded']} (present {stats['ref_present']}) | "
        f"hash_mismatch {stats['hash_mismatch']} | errors {stats['error']}"
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
        description="Seed versions + refs from the existing folios rows (Phase 2, commit A)."
    )
    p.add_argument("db_path", type=Path, nargs="?", help="A single skein.db to migrate")
    p.add_argument("--all", action="store_true", help="Every registered project")
    p.add_argument("--dry-run", action="store_true", help="Read-only; count only")
    p.add_argument("--backup", action="store_true",
                   help="Back up each db (online-backup API) before applying")
    p.add_argument("--mapping", type=Path, help="JSONL seed mapping (single db)")
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
    print(f"[{mode}] seed versions+refs over {len(targets)} db(s)")

    grand = {k: 0 for k in (*STAT_KINDS, "total", "versions_written", "refs_written")}
    failures = []
    mapping_failures = []
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
