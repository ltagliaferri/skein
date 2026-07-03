#!/usr/bin/env python3
"""Phase 3b post-swap STRUCTURAL verifier (sibling of verify_versions_refs.py and
verify_threads_control.py).

Read-only, self-contained on a SINGLE post-swap db — no before-image needed. It
proves the ``thread_hash`` PK swap (design §4.4) landed structurally sound. The
DIFFERENTIAL equivalence legs (control read unchanged, Class B display unchanged,
row-count reconciliation) are before/after comparisons the migration harness holds
the pre-image for, so they live in the test specs, not here.

Checks per db (any divergence is a BLOCKER; exits non-zero) — design §8:
  1. ``thread_hash`` is the declared PRIMARY KEY and is UNIQUE (no surviving
     duplicate). An unindexed / non-PK table would pass every value check below,
     so the schema is asserted, not assumed.
  2. Self-verification: ``compute_thread_hash(row)`` equals the stored ``thread_hash``
     for every row (catches a re-anchor that moved endpoints but not the hash, or a
     tampered field).
  3. No dangling endpoint: every ``sha256::``-shaped endpoint (a content-hash
     address — a Class B / version-anchored / control-genesis edge) resolves in
     ``versions``. Non-hash endpoints (Class C / orphan slugs, assignment agents)
     are NOT required to resolve — an orphan is deliberately KEPT, not an error
     (§5.3), so requiring slug resolution here would wrongly block a healthy db.
  4. All six ``idx_threads_*`` present (an unindexed hot table passes value checks).
  5. I1 holds on the post db: no ``genesis_hash`` shared across two ``refs.slug`` —
     the invariant the re-anchor rested on, re-checked after (defense in depth).

Usage:
    python -m skein.migrations.verify_threads_pk PATH/skein.db
    python -m skein.migrations.verify_threads_pk --all
    python -m skein.migrations.verify_threads_pk --all --sample 200   # faster
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skein.identity import compute_thread_hash  # noqa: E402
from skein.storage import load_project_registry  # noqa: E402

_HASH_PREFIX = "sha256::"

# The six schema-global thread indexes the swap must have recreated after the
# DROP+RENAME (design §4.4 step 5 / §8). Kept in sync with storage.py and the swap.
_EXPECTED_INDEXES = (
    "idx_threads_from", "idx_threads_to", "idx_threads_type", "idx_threads_created",
    "idx_threads_to_type_created", "idx_threads_from_type_created",
)


def _is_hash_shaped(endpoint) -> bool:
    """True if ``endpoint`` is a content-hash address (``sha256::<hex>``). These MUST
    resolve in ``versions``; slugs (``type-date-xxxx``) and agent ids never carry the
    prefix, so a Class C / orphan endpoint is correctly excluded from the dangling
    check (§5.3 — orphans are kept, not dangling)."""
    return isinstance(endpoint, str) and endpoint.startswith(_HASH_PREFIX)


def verify_db(db_path, *, sample: int = 0) -> Tuple[List[str], List[str]]:
    """Return (problems, warnings). ``problems`` are BLOCKERS. ``sample`` limits the
    self-verification / dangling scan (0 == every row, the default for a gate).

    Self-contained on a single post-swap db; opens read-only (``mode=ro``), so it
    never mutates the input and is safe on a live WAL db.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    problems: List[str] = []
    warnings: List[str] = []
    try:
        versions = {r["content_hash"]
                    for r in conn.execute("SELECT content_hash FROM versions")}

        # 1. thread_hash is the sole PK and is UNIQUE.
        pk_cols = [r[1] for r in conn.execute("PRAGMA table_info(threads)") if r[5]]
        if pk_cols != ["thread_hash"]:
            problems.append(
                f"thread_hash is not the sole primary key (pk columns: {pk_cols}) — "
                f"the swap's schema post-condition failed")
        for d in conn.execute(
            "SELECT thread_hash, COUNT(*) AS c FROM threads "
            "GROUP BY thread_hash HAVING c > 1"
        ):
            problems.append(
                f"duplicate thread_hash {d['thread_hash']!r} ({d['c']} rows) — "
                f"thread_hash is not UNIQUE")

        # 2 + 3. self-verification and no dangling endpoint, per row.
        rows = conn.execute(
            "SELECT thread_id, from_id, to_id, type, content, weaver, created_at, "
            "thread_hash FROM threads").fetchall()
        if sample:
            rows = rows[:sample]
        for row in rows:
            recomputed = compute_thread_hash(
                row["from_id"], row["to_id"], row["type"], row["weaver"],
                row["created_at"], row["content"])
            if recomputed != row["thread_hash"]:
                problems.append(
                    f"thread {row['thread_id']!r} does not self-verify (stored "
                    f"{row['thread_hash']!r}, recomputed {recomputed!r})")
            for col in ("from_id", "to_id"):
                endpoint = row[col]
                if _is_hash_shaped(endpoint) and endpoint not in versions:
                    problems.append(
                        f"dangling endpoint: {col} {endpoint!r} is a content-hash "
                        f"address not in versions (thread {row['thread_id']!r})")

        # 4. all six idx_threads_* present.
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for name in _EXPECTED_INDEXES:
            if name not in indexes:
                problems.append(
                    f"missing index {name} (an unindexed hot table passes every value "
                    f"check — schema-global-name recreation trap, §4.4 step 5)")

        # 5. I1: genesis unique per lineage on the post db.
        seen: dict = {}
        for slug, genesis in conn.execute("SELECT slug, genesis_hash FROM refs"):
            if genesis is None:
                continue
            if genesis in seen and seen[genesis] != slug:
                problems.append(
                    f"I1 violation: genesis_hash {genesis!r} shared by lineages "
                    f"{seen[genesis]!r} and {slug!r}")
            else:
                seen.setdefault(genesis, slug)

        return problems, warnings
    finally:
        conn.close()


def _db_paths_from_registry() -> list:
    out = []
    for pid, entry in load_project_registry().items():
        if not isinstance(entry, dict):
            continue
        data_dir = entry.get("data_dir")
        base = entry.get("path")
        if data_dir:
            db = Path(data_dir) / "skein.db"
        elif base:
            db = Path(base) / ".skein" / "data" / "skein.db"
        else:
            continue
        if db.exists():
            out.append((pid, db))
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description="Phase 3b post-swap thread_hash-PK structural verifier.")
    p.add_argument("db_path", type=Path, nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--sample", type=int, default=0, metavar="N",
                   help="Self-verify only the first N threads per db (default: all)")
    args = p.parse_args()

    if bool(args.all) == bool(args.db_path):
        p.error("give exactly one of: a db_path, or --all")

    targets = (_db_paths_from_registry() if args.all
               else [(str(args.db_path), args.db_path)])
    bad = 0
    for label, db in targets:
        try:
            problems, warnings = verify_db(db, sample=args.sample)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {label}: {type(e).__name__}: {e}")
            bad += 1
            continue
        if problems:
            bad += 1
            print(f"  PROBLEMS {label}: {len(problems)} problem(s)")
            for msg in problems[:20]:
                print(f"      - {msg}")
            if len(problems) > 20:
                print(f"      ... and {len(problems) - 20} more")
        else:
            print(f"  OK {label}: thread_hash-PK structurally sound")
        for msg in warnings:
            print(f"      ! WARN {label}: {msg}")

    if bad:
        print(f"\n{bad} db(s) FAILED the thread_hash-PK structural check")
        return 1
    print("\nAll clean — thread_hash-PK structurally sound.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
