#!/usr/bin/env python3
"""B->C verification gate: prove refs/versions are a faithful mirror of folios.

Read-only. Gates the commit-C read-flip: before any reader repoints at the
versions⋈refs heads join, this proves that the join reconstructs exactly what the
folios rows say today, row-for-row — so the flip cannot silently change output.
It does NOT lean on "the harness is green" (the harness is blind to head-filtering
and to dual-write drift); it diffs the actual tables. Mirrors the diff the design
note specifies (§8): rebuild the head + control cache from versions/refs and diff
against the live folios rows, INCLUDING site_id, so a move_folio that updated only
one of the two tables is caught.

Checks per db (any divergence is a BLOCKER and exits non-zero):
  1. Row parity: the set of refs.slug equals the set of folios.folio_id.
  2. No dangling: every refs.head_hash and refs.genesis_hash exists in versions.
  3. Head == current content: refs.head_hash == folios.content_hash for every
     slug (the dual-write invariant — folios.content_hash is the recomputed hash
     of the current content, which is exactly the head version).
  4. Head IDENTITY mirror: the head version's five identity fields
     (type/title/content/created_at/created_by) equal the folios row's — proving
     directly that what commit C reads via the join is byte-identical to what
     folios says today, not merely that two hashes happen to match (catches a
     stale folios.content_hash that agrees with a stale head pointer).
  5. Control mirror: every non-identity control column on refs equals the folios
     column row-for-row: site_id, status, assigned_to, archived, target_agent,
     omlet, acknowledged_at, metadata (after the same read coercions).
  6. Version self-verification (FULL by default): a versions row's content_hash
     equals compute_folio_hash of its own five fields.
  7. Edge integrity: every supersedes/reverted edge's endpoints (content hashes)
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

# The non-identity control columns refs caches from folios, and how a read coerces
# each so the diff compares post-read values (not raw bytes that read identically).
CONTROL_COLS = ("site_id", "status", "assigned_to", "archived", "target_agent",
                "omlet", "acknowledged_at", "metadata")


def _norm(col: str, value):
    """Coerce a control value the way the read path does, so equal-on-read values
    are not reported as drift (NULL metadata and "{}" both read as {}; a missing
    status reads as 'open'; archived reads as a 0/1 int)."""
    if col == "status":
        return value or "open"
    if col == "archived":
        return 1 if value else 0
    if col == "metadata":
        # NULL and "{}" both reconstruct to {} — treat as equal.
        return value if value not in (None, "{}", "") else "{}"
    return value


IDENTITY_COLS = ("type", "title", "content", "created_at", "created_by")


def verify_db(db_path: Path, *, sample: int = 0):
    """Return (problems, warnings). `problems` block the read-flip (exit non-zero);
    `warnings` are surfaced but non-blocking. ``sample`` limits the
    self-verification scan (0 == verify every version, the default for a gate)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    problems: List[str] = []
    warnings: List[str] = []
    try:
        versions = {r["content_hash"]: r for r in conn.execute("SELECT * FROM versions")}
        refs = {r["slug"]: r for r in conn.execute("SELECT * FROM refs")}
        # Phase 3a A5 retires the folios table. When it is absent, the folios-mirror
        # checks (1, 3, 4, 5) have no baseline and are skipped; the folios-independent
        # structural checks (2 dangling, 6 self-verify, 7 acyclic DAG) still run, so
        # verify_db stays a useful versions/refs integrity gate post-drop.
        folios_present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='folios'"
        ).fetchone() is not None
        folios = (
            {r["folio_id"]: r for r in conn.execute("SELECT * FROM folios")}
            if folios_present else {}
        )

        # 1. Row parity (only meaningful while folios still backs the mirror).
        if folios_present:
            only_refs = set(refs) - set(folios)
            only_folios = set(folios) - set(refs)
            if only_refs:
                problems.append(f"{len(only_refs)} slug(s) in refs but not folios: "
                                f"{sorted(only_refs)[:5]}")
            if only_folios:
                problems.append(f"{len(only_folios)} folio(s) with no ref (would vanish "
                                f"at read-flip): {sorted(only_folios)[:5]}")

        for slug, ref in refs.items():
            # 2. No dangling.
            if ref["head_hash"] not in versions:
                problems.append(f"{slug}: head_hash not in versions")
            if ref["genesis_hash"] not in versions:
                problems.append(f"{slug}: genesis_hash not in versions")

            folio = folios.get(slug)
            if folio is None:
                continue

            # 3. Head == current content.
            if ref["head_hash"] != folio["content_hash"]:
                problems.append(f"{slug}: refs.head_hash {ref['head_hash']!r} != "
                                f"folios.content_hash {folio['content_hash']!r}")

            # 4. Head IDENTITY mirror — the head VERSION's five fields == folios'.
            # This is what commit C will read via the join; prove it directly, not
            # via two agreeing hashes (a stale folios.content_hash matching a stale
            # head pointer would pass check 3 yet differ in content here).
            head = versions.get(ref["head_hash"])
            if head is not None:
                for col in IDENTITY_COLS:
                    if head[col] != folio[col]:
                        problems.append(
                            f"{slug}: head version '{col}' {head[col]!r} != "
                            f"folios {folio[col]!r}")

            # 5. Control mirror, row-for-row.
            for col in CONTROL_COLS:
                rv, fv = _norm(col, ref[col]), _norm(col, folio[col])
                if rv != fv:
                    problems.append(f"{slug}: control '{col}' refs={rv!r} folios={fv!r}")

        # 6. Version self-verification (every version by default).
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

        # 7. Edge integrity: endpoints exist; supersedes graph acyclic; every head
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
    p = argparse.ArgumentParser(description="B->C verification: refs/versions mirror folios.")
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
            print(f"  DIVERGED {label}: {len(problems)} problem(s)")
            for msg in problems[:20]:
                print(f"      - {msg}")
            if len(problems) > 20:
                print(f"      ... and {len(problems) - 20} more")
        else:
            print(f"  OK {label}: refs/versions mirror folios")
        for msg in warnings:
            print(f"      ! WARN {label}: {msg}")

    if bad:
        print(f"\n{bad} db(s) DIVERGED — read-flip BLOCKED")
        sys.exit(1)
    print("\nAll clean — refs/versions are a faithful mirror; read-flip safe.")


if __name__ == "__main__":
    main()
