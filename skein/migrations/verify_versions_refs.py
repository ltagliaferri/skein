#!/usr/bin/env python3
"""versions/refs structural integrity checker.

Read-only. Originally the B->C read-flip gate that proved refs/versions were a
faithful mirror of the legacy `folios` table, row-for-row, before the reader
repointed at the versions⋈refs heads join. That folios-mirror gate is RETIRED as
of Phase 3a A5: A5 stopped writing `folios` (save_folio/move_folio no longer touch
it), so a `folios` table — where one still exists in the Part 1→Part 2 window — is
a frozen, non-authoritative vestige. Diffing refs against it would report false
divergences on a healthy live db (refs advances; folios does not). The
folios-mirror checks are therefore removed; what remains are the checks that
depend only on versions/refs/threads and stay valid forever.

Checks per db (any divergence is a BLOCKER and exits non-zero):
  1. No dangling: every refs.head_hash and refs.genesis_hash exists in versions.
  2. Version self-verification (FULL by default): a versions row's content_hash
     equals compute_folio_hash of its own five fields.
  3. Edge integrity: every supersedes/reverted edge's endpoints (content hashes)
     exist in versions; the supersedes graph is acyclic (a cycle is a BLOCKER).
     A genesis->head chain gap is reported as a WARNING, not a blocker: the global
     content DAG cannot represent a converge-then-diverge cross-lineage edit as
     both acyclic and per-genesis reachable, so a gap is sometimes unavoidable and
     never affects reads (the head predicate is per-ref, never edge-walked). It is
     surfaced so a window-repair gap (a real bug) is still visible to the operator.

Usage:
    python -m skein.migrations.verify_versions_refs PATH/skein.db
    python -m skein.migrations.verify_versions_refs --all
    python -m skein.migrations.verify_versions_refs --all --sample 200   # faster
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List

from skein.identity import compute_folio_hash
from skein.storage import load_project_registry


def verify_db(db_path: Path, *, sample: int = 0):
    """Return (problems, warnings). `problems` are BLOCKERS (exit non-zero);
    `warnings` are surfaced but non-blocking. ``sample`` limits the
    self-verification scan (0 == verify every version, the default for a gate).

    Post-A5 this checks only versions/refs/threads structural integrity; the
    legacy folios-mirror diff is retired (folios is no longer written)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    problems: List[str] = []
    warnings: List[str] = []
    try:
        versions = {r["content_hash"]: r for r in conn.execute("SELECT * FROM versions")}
        refs = {r["slug"]: r for r in conn.execute("SELECT * FROM refs")}

        # 1. No dangling: every refs head/genesis hash resolves to a version.
        for slug, ref in refs.items():
            if ref["head_hash"] not in versions:
                problems.append(f"{slug}: head_hash not in versions")
            if ref["genesis_hash"] not in versions:
                problems.append(f"{slug}: genesis_hash not in versions")

        # 2. Version self-verification (every version by default).
        items = list(versions.values())
        if sample:
            items = items[:sample]
        for v in items:
            h = compute_folio_hash({
                "type": v["type"], "title": v["title"], "content": v["content"],
                "created_at": v["created_at"], "created_by": v["created_by"],
            })
            if h != v["content_hash"]:
                problems.append(f"version {v['content_hash']!r} does not self-verify "
                                f"(recomputed {h!r})")

        # 3. Edge integrity: endpoints exist; supersedes graph acyclic; every head
        # reachable from its genesis.
        succ = {}  # old_hash -> [new_hash] (supersedes: from=new, to=old)
        for e in conn.execute(
            "SELECT from_id, to_id, type FROM threads WHERE type IN ('supersedes','reverted')"
        ):
            if e["from_id"] not in versions:
                problems.append(f"{e['type']} edge from_id {e['from_id']!r} not in versions")
            if e["to_id"] not in versions:
                problems.append(f"{e['type']} edge to_id {e['to_id']!r} not in versions")
            if e["type"] == "supersedes":
                succ.setdefault(e["to_id"], []).append(e["from_id"])

        # Cycle detection over the supersedes DAG. ITERATIVE DFS (explicit stack of
        # per-node iterators) — a recursive walk would hit Python's recursion limit
        # on a deep-but-valid lineage and falsely ERROR-block a structurally sound db.
        WHITE, GREY, BLACK = 0, 1, 2
        color = {}
        cycle = False
        for root in list(succ.keys()):
            if color.get(root, WHITE) != WHITE:
                continue
            color[root] = GREY
            stack = [(root, iter(succ.get(root, ())))]
            while stack and not cycle:
                node, it = stack[-1]
                nxt = next(it, None)
                if nxt is None:
                    color[node] = BLACK
                    stack.pop()
                    continue
                c = color.get(nxt, WHITE)
                if c == GREY:          # back edge -> cycle
                    cycle = True
                elif c == WHITE:
                    color[nxt] = GREY
                    stack.append((nxt, iter(succ.get(nxt, ()))))
            if cycle:
                break
        if cycle:
            problems.append("supersedes graph has a cycle")

        # Every head reachable from its genesis along supersedes edges (catches a
        # window-repaired lineage left with a genesis/head gap and no connecting
        # edge). A single-version lineage (genesis == head) is trivially reachable.
        gap = 0
        for slug, ref in refs.items():
            g, h = ref["genesis_hash"], ref["head_hash"]
            if g == h:
                continue
            seen, stack = set(), [g]
            while stack:
                n = stack.pop()
                if n in seen:
                    continue
                seen.add(n)
                stack.extend(succ.get(n, []))
            if h not in seen:
                gap += 1
        if gap:
            warnings.append(f"{gap} lineage(s) whose head is not reachable from "
                            f"genesis along supersedes edges (chain gap — expected "
                            f"for converge/diverge cross-lineage edits; a "
                            f"window-repair gap here would be a real bug)")

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


def main() -> None:
    p = argparse.ArgumentParser(
        description="versions/refs structural integrity check.")
    p.add_argument("db_path", type=Path, nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--sample", type=int, default=0, metavar="N",
                   help="Self-verify only the first N versions per db (default: all)")
    args = p.parse_args()

    if bool(args.all) == bool(args.db_path):
        p.error("give exactly one of: a db_path, or --all")

    targets = _db_paths_from_registry() if args.all else [(str(args.db_path), args.db_path)]
    bad = 0
    for label, db in targets:
        try:
            problems, warnings = verify_db(db, sample=args.sample)
        except Exception as e:
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
            print(f"  OK {label}: versions/refs structurally sound")
        for msg in warnings:
            print(f"      ! WARN {label}: {msg}")

    if bad:
        print(f"\n{bad} db(s) FAILED the versions/refs integrity check")
        sys.exit(1)
    print("\nAll clean — versions/refs structurally sound.")


if __name__ == "__main__":
    main()
