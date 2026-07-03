#!/usr/bin/env python3
"""Phase 3b — the thread_hash PK swap + Class B structural re-anchor (design §4-§8).

This is the destructive live migration that makes ``threads.thread_hash`` the
primary key, re-anchors the folio↔folio "Class B" structural edges from slugs to
content hashes, and collapses true byte-duplicates. It is written to the contract
pinned in ``tests/test_phase3b_threads.py`` and driven by ``docs/PHASE_3B_DESIGN.md``.

This module lands in increments (each felled): the per-row classifier and the
manifest-admission predicate first (pure functions, §5 / §7), then ``migrate_db``
(the table rewrite, §4.4), then the verifier (a sibling module).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Make the repo root importable so a direct
# ``python skein/migrations/threads_pk_swap.py`` (not just ``-m``) works.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skein.identity import compute_thread_hash  # noqa: E402

# Control types are class A by TYPE regardless of endpoints (design §3.1 / §5):
# the server mints them and they drive refs filters. The genesis-anchored Class B
# set is ALSO imported from storage (not re-declared) so the migration's re-anchor
# set and the reader's genesis→slug rewrite set (design §6/§6.1) are ONE source of
# truth — a drift between them would silently corrupt the display round-trip across
# the swap (a genesis-anchored edge re-keyed by the migration but not rewritten back
# by the reader would drop out of a slug query). Only `reference` was exercised
# end-to-end before, so such a drift on mention/reply/succession/within could ship
# green (fell:phase3b-impl:r5, opus).
from skein.storage import (  # noqa: E402
    CLASS_B_GENESIS_DISPLAY_TYPES,
    CONTROL_THREAD_TYPES,
)

# Types that are never structural folio↔folio edges even when both endpoints
# happen to resolve to a folio (design §5.2): a tag is folio→label, a message is
# folio commentary. Their F→F rows are degenerate self-loops, not edges.
SEMANTIC_C_TYPES = ("tag", "message")

# The Class B types whose endpoints are anchored on the lineage GENESIS hash and are
# re-keyed slug→genesis at the swap (design §6) — the SAME set the reader rewrites
# back to slugs (imported above, single source). `within` is named here ahead of its
# ThreadType enum addition (deferred to first use, design §9 #2 — see the tripwire
# test_within_published_deferred_from_threadtype_enum); it has 0 rows and the Thread
# model rejects it, so the migration never actually encounters one.
GENESIS_ANCHORED_TYPES = CLASS_B_GENESIS_DISPLAY_TYPES

# The version-anchored structural types (design §6): edit-DAG / publish edges whose
# BOTH endpoints are, by construction, exact ``versions.content_hash`` values. The
# post-swap verifier requires both endpoints resolve in ``versions`` for these types
# (a miss is a genuine dangling BLOCKER), while a hash-shaped endpoint on any OTHER
# type may be a kept orphan (§5.3), not an error. `published` is likewise named ahead
# of its enum (§9 #2): it must stay OUT of ThreadType until its write path guarantees
# version-keyed endpoints, or a slug-keyed `published` posted via POST /threads would
# be kept slug-keyed by the swap (version-anchored → not re-anchored) and then blocked
# by the verifier as dangling.
VERSION_ANCHORED_TYPES = ("supersedes", "reverted", "published")

# The manifest allow-list (design §7): the structural edge types that may federate.
# An ALLOW-LIST, not a denylist — an unknown/new type must be rejected, never
# silently federated. DERIVED from the genesis + version partition (not hand-listed)
# so it can never drift out of sync with either half.
STRUCTURAL_TYPES = tuple(GENESIS_ANCHORED_TYPES) + VERSION_ANCHORED_TYPES

# The six schema-global thread indexes, recreated on the renamed table AFTER the
# DROP+RENAME (design §4.4 step 5) — the names are schema-global, so they cannot be
# created on ``threads_new`` while the old ``threads`` still holds them. Kept
# byte-identical to storage.py's CREATE INDEX statements (one source of shape).
_THREAD_INDEXES: Tuple[str, ...] = (
    "CREATE INDEX idx_threads_from ON threads(from_id)",
    "CREATE INDEX idx_threads_to ON threads(to_id)",
    "CREATE INDEX idx_threads_type ON threads(type)",
    "CREATE INDEX idx_threads_created ON threads(created_at)",
    "CREATE INDEX idx_threads_to_type_created ON threads(to_id, type, created_at DESC)",
    "CREATE INDEX idx_threads_from_type_created ON threads(from_id, type, created_at DESC)",
)

# The §3 end-state schema: thread_hash is the PK; thread_id is KEPT (NOT NULL,
# non-PK) as the reader tiebreaker + audit handle (design §4.2). Table only — the
# indexes are recreated after the rename.
_THREADS_NEW_DDL = """
    CREATE TABLE threads_new (
        thread_hash TEXT PRIMARY KEY,
        thread_id   TEXT NOT NULL,
        from_id     TEXT NOT NULL,
        to_id       TEXT NOT NULL,
        type        TEXT NOT NULL,
        content     TEXT,
        weaver      TEXT,
        created_at  DATETIME NOT NULL
    )
"""

BUSY_TIMEOUT_MS = 30000
# The quiescence probe's write-lock acquisition budget. A truly quiesced db yields
# the write lock in microseconds; a live skein.service writer holds it, so a small
# budget fails fast without false-positiving on transient filesystem contention.
QUIESCE_PROBE_MS = 2000


class PreconditionError(RuntimeError):
    """Typed refusal: ``migrate_db`` raises this when a db fails a fail-closed
    precondition (an I1 genesis collision, a re-anchor collision that would collapse
    two DISTINCT structural edges, a non-quiesced service, or a pre-existing backup)
    — so a caller can distinguish a deliberate refusal from an incidental crash
    (design §4.4 / §5.3)."""


def classify_row(row, *, slugs, versions) -> str:
    """Classify one thread row as ``'A'`` (control), ``'B'`` (structural folio↔folio
    edge), or ``'C'`` (non-federating), per design §3.1/§5. ``row`` carries
    ``from_id``/``to_id``/``type``; ``slugs`` is the set of live ``refs.slug`` and
    ``versions`` the set of ``versions.content_hash``.

    Order matters: control wins by type (an ``assignment``'s agent ``to_id`` would
    otherwise read non-folio), then the semantic-C overrides and the self-loop and
    orphan tests demote to C; only a distinct, both-endpoints-folio, non-override row
    is B.
    """
    ttype = row["type"]
    from_id, to_id = row["from_id"], row["to_id"]

    # 1. control is class A by type, regardless of endpoints (control precedence).
    if ttype in CONTROL_THREAD_TYPES:
        return "A"
    # 2. tag / message are C even when both endpoints resolve to a folio.
    if ttype in SEMANTIC_C_TYPES:
        return "C"
    # 3. a self-loop is not a structural edge between two folios.
    if from_id == to_id:
        return "C"
    # 4. an orphan endpoint (resolves to neither a live slug nor a version) → C;
    #    kept-and-logged, never re-anchored (§5.3).
    folio_ids = slugs | versions
    if from_id not in folio_ids or to_id not in folio_ids:
        return "C"
    # 5. distinct, both-folio, and a NAMED structural type → class B. The B decision
    #    is an ALLOW-LIST (the §5 B-eligible set == STRUCTURAL_TYPES), symmetric with
    #    manifest_eligible: an unknown/new type falls through to C (not re-anchored),
    #    the fail-safe default, so adding a ThreadType without updating this module
    #    can never silently re-key a row it shouldn't.
    return "B" if ttype in STRUCTURAL_TYPES else "C"


def manifest_eligible(row, *, versions) -> bool:
    """The Merkle-manifest admission predicate (design §7), an explicit ALLOW-LIST.
    True iff the row is a structural type, connects two DISTINCT folios, and BOTH
    endpoints resolve in the (append-only) ``versions`` set. Class A control
    self-loops also carry version-hash endpoints, so both the type gate and the
    ``from != to`` gate are load-bearing.
    """
    if row["type"] not in STRUCTURAL_TYPES:
        return False
    from_id, to_id = row["from_id"], row["to_id"]
    if from_id == to_id:
        return False
    return from_id in versions and to_id in versions


# ══════════════════════════════════════════════════════════════════════════════
# The migration (design §4.4) — the thread_hash PK swap + Class B re-anchor.
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PKSwapResult:
    """Observable outcome of one ``migrate_db`` run.

    - ``reanchored``: Class B rows whose endpoints actually moved slug→genesis
      (version-anchored edges, already hash-keyed, are not counted).
    - ``collapsed``: the ``thread_hash`` of each row dropped as a true byte-duplicate
      (§2.1). ``len(collapsed)`` is exactly the row-count delta — the ONLY loss the
      swap accepts (a re-anchor collision raises instead of collapsing, §4.4/§5.3).
    - ``orphans``: Class C rows kept-and-logged because an endpoint resolves to no
      folio (§5.3) — never dropped, never re-anchored.
    """
    reanchored: int = 0
    collapsed: List[str] = field(default_factory=list)
    orphans: List[dict] = field(default_factory=list)
    dry_run: bool = False
    already_migrated: bool = False


def _ref_maps(conn: sqlite3.Connection) -> Tuple[Set[str], Dict[str, str], Dict[str, str]]:
    """(live slugs, slug→genesis, genesis→slug) from ``refs``. Asserts I1 (genesis
    unique per lineage) — a violation is a ``PreconditionError``, never assumed
    (design §4.4). Mirrors ``migrate_threads_control._ref_maps`` so the re-anchor and
    the 3a control migration read refs identically."""
    slugs: Set[str] = set()
    slug_to_genesis: Dict[str, str] = {}
    genesis_to_slug: Dict[str, str] = {}
    for slug, genesis in conn.execute("SELECT slug, genesis_hash FROM refs"):
        slugs.add(slug)
        slug_to_genesis[slug] = genesis
        if genesis is not None:
            if genesis in genesis_to_slug and genesis_to_slug[genesis] != slug:
                raise PreconditionError(
                    f"I1 violation: genesis_hash {genesis!r} shared by lineages "
                    f"{genesis_to_slug[genesis]!r} and {slug!r} — the re-anchor rests "
                    f"on genesis being unique per lineage (§4.4); refusing")
            genesis_to_slug[genesis] = slug
    return slugs, slug_to_genesis, genesis_to_slug


def _versions_set(conn: sqlite3.Connection) -> Set[str]:
    return {r[0] for r in conn.execute("SELECT content_hash FROM versions")}


def _thread_pk_columns(conn: sqlite3.Connection) -> List[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(threads)") if r[5]]


def _already_migrated(conn: sqlite3.Connection) -> bool:
    """True iff ``threads`` is already at the §3 end-state (``thread_hash`` is the
    sole PK). Checked FIRST so a re-run short-circuits to a no-op PASS BEFORE the
    no-prior-backup precondition (design idempotency note / §8)."""
    return _thread_pk_columns(conn) == ["thread_hash"]


def _null_thread_id_count(conn: sqlite3.Connection) -> int:
    """Count rows with a NULL ``thread_id``. SQLite does NOT enforce NOT-NULL on a
    non-INTEGER ``PRIMARY KEY`` column, so the legacy ``thread_id TEXT PRIMARY KEY``
    can hold NULLs. Since ``threads_new.thread_id`` IS ``NOT NULL``, such a row would
    be silently swallowed on copy and lost — refuse the db up front instead."""
    return conn.execute(
        "SELECT COUNT(*) FROM threads WHERE thread_id IS NULL").fetchone()[0]


def _backup_copy(src: Path, dst: Path) -> None:
    """Consistent snapshot src → dst via SQLite's online-backup API (torn-read-safe,
    includes committed WAL frames), matching drop_folios/cutover_threads_control. The
    read connection is independent, so this is safe to call while the migration
    connection holds ``BEGIN IMMEDIATE`` (a write lock with no writes yet): in WAL the
    read sees the committed pre-swap snapshot, and the held lock guarantees no writer
    can slip a commit in between the backup and the rewrite (the restore point is a
    faithful pre-swap image)."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def _reanchor_endpoints(
    from_id: str, to_id: str, ttype: str, slug_to_genesis: Dict[str, str]
) -> Tuple[str, str]:
    """Re-key a genesis-anchored Class B row's endpoints slug→genesis (design §6).
    A version-anchored type is left byte-faithful (its endpoints are already content
    hashes). ``slug_to_genesis.get(x, x)`` leaves a non-slug endpoint (already a
    hash) unchanged, so the map is safe even on a mixed-key row."""
    if ttype in GENESIS_ANCHORED_TYPES:
        return slug_to_genesis.get(from_id, from_id), slug_to_genesis.get(to_id, to_id)
    return from_id, to_id


def _plan(
    conn: sqlite3.Connection, slugs: Set[str], slug_to_genesis: Dict[str, str],
    versions: Set[str],
) -> Tuple[List[tuple], PKSwapResult]:
    """Classify + re-anchor every thread row, returning (survivors, result).

    ``survivors`` are the post-swap rows to INSERT (thread_hash, thread_id, from_id,
    to_id, type, content, weaver, created_at). A byte-duplicate (two rows identical
    on all six canonical fields, §2.1) collapses to one and its hash is logged in
    ``result.collapsed``. A re-anchor COLLISION — two rows that were DISTINCT
    pre-anchor but map to the same post-anchor hash — can only arise under an I1
    violation (ruled out by the precondition) and is a ``PreconditionError`` BLOCKER,
    never a benign drop of a non-regenerable structural edge (§4.4/§5.3).
    """
    result = PKSwapResult()
    folio_ids = slugs | versions
    seen: Dict[str, tuple] = {}           # new_hash → the ORIGINAL canonical tuple
    survivors: List[tuple] = []
    # A stable order so the row kept from a byte-dup group is deterministic (design
    # §4.3 "keeps the original row"). WHICH thread_id survives is immaterial: a
    # byte-dup group is identical on all six canonical fields (incl. content and
    # created_at), so the reduced control value is the same whichever row survives —
    # the order only pins determinism, not a semantically-preferred winner.
    rows = conn.execute(
        "SELECT thread_id, from_id, to_id, type, content, weaver, created_at "
        "FROM threads ORDER BY created_at, thread_id"
    ).fetchall()
    for row in rows:
        ttype = row["type"]
        from_id, to_id = row["from_id"], row["to_id"]
        weaver, created_at, content = row["weaver"], row["created_at"], row["content"]
        klass = classify_row(row, slugs=slugs, versions=versions)

        new_from, new_to = from_id, to_id
        if klass == "B":
            new_from, new_to = _reanchor_endpoints(
                from_id, to_id, ttype, slug_to_genesis)
            if (new_from, new_to) != (from_id, to_id):
                result.reanchored += 1
        elif (klass == "C" and ttype in STRUCTURAL_TYPES and from_id != to_id
              and (from_id not in folio_ids or to_id not in folio_ids)):
            # A structural-type row demoted to C by an UNRESOLVED endpoint is an
            # orphan: kept slug-keyed and logged, never dropped (§5.3). (A self-loop
            # or a tag/message C is a semantic C, not an orphan.)
            result.orphans.append({"thread_id": row["thread_id"], "type": ttype,
                                   "from_id": from_id, "to_id": to_id})

        # Recompute the identity over the (possibly re-anchored) endpoints for EVERY
        # row, so every survivor self-verifies post-swap by construction. A row whose
        # endpoints did not move recomputes to its existing hash (save_thread stamped
        # it the same way), so this is a no-op for A/C/version-anchored rows.
        new_hash = compute_thread_hash(
            new_from, new_to, ttype, weaver, created_at, content)
        orig_key = (from_id, to_id, ttype, weaver, created_at, content)
        if new_hash in seen:
            if seen[new_hash] == orig_key:
                result.collapsed.append(new_hash)   # true byte-duplicate → collapse
                continue
            raise PreconditionError(
                f"re-anchor collision: two DISTINCT edges collapse to {new_hash} "
                f"(only possible under an I1 violation) — refusing rather than drop a "
                f"non-regenerable structural edge (§4.4/§5.3)")
        seen[new_hash] = orig_key
        survivors.append((new_hash, row["thread_id"], new_from, new_to, ttype,
                          content, weaver, created_at))
    return survivors, result


def migrate_db(db_path, *, dry_run: bool = False) -> PKSwapResult:
    """Rewrite ``threads`` with ``thread_hash`` as the primary key, re-anchoring the
    Class B structural edges slug→genesis and collapsing true byte-duplicates, in one
    transaction (design §4.4). Returns a :class:`PKSwapResult`.

    Idempotent by short-circuit: a db already at the PK schema is a no-op PASS,
    checked BEFORE the no-prior-backup precondition, so a re-run neither refuses nor
    rewrites. ``dry_run=True`` mutates nothing (no backup, no writes) and returns the
    same plan (counts). Preconditions (fail-closed, only on a not-yet-migrated db):
    ``skein.service`` quiesced (proven by acquiring the write lock), I1 (genesis
    unique per lineage), no NULL ``thread_id``, no prior backup — each a
    :class:`PreconditionError`.

    Quiescence and atomicity are ONE held write lock. The migration connection takes
    ``BEGIN IMMEDIATE`` (a short-budget acquire — a live skein.service writer holds
    the lock and trips it → refusal) and holds it across the I1/thread_id/no-backup
    checks, the backup, AND the rewrite, releasing only at COMMIT. So the backup is a
    faithful pre-swap image (no writer can commit between the snapshot and the swap)
    and no live write is ever lost to a later restore — closing the probe-then-backup
    TOCTOU a separate quiescence probe would leave open.
    """
    db_path = Path(db_path)

    # 0. Idempotency short-circuit — a completed swap is a no-op PASS, BEFORE any
    #    precondition (so a re-run does not refuse on its own prior backup).
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        if _already_migrated(ro):
            return PKSwapResult(dry_run=dry_run, already_migrated=True)
    finally:
        ro.close()

    if dry_run:
        # Read-only: assert every refusal the live run would, compute the plan, mutate
        # nothing. Safe on a live WAL db — it is the copy-proof's plan-only pass. It
        # surfaces the SAME refusals the live run raises (I1, NULL thread_id, re-anchor
        # collision) so a dry-run over --all before --live never hides a db that will
        # be refused.
        ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            slugs, slug_to_genesis, _ = _ref_maps(ro)   # asserts I1
            n_null = _null_thread_id_count(ro)
            if n_null:
                raise PreconditionError(
                    f"{n_null} thread row(s) with a NULL thread_id — threads_new."
                    f"thread_id is NOT NULL, so these would be silently lost on copy; "
                    f"resolve by hand before migrating")
            versions = _versions_set(ro)
            _, result = _plan(ro, slugs, slug_to_genesis, versions)
            result.dry_run = True
            return result
        finally:
            ro.close()

    # LIVE path. Acquire the write lock FIRST (isolation_level=None so we drive
    # BEGIN/COMMIT ourselves), with a short busy_timeout so an unquiesced writer trips
    # it fast → a clean refusal rather than a mid-transform busy error. Every
    # precondition, the backup, and the rewrite then run under this one lock.
    conn = sqlite3.connect(str(db_path), isolation_level=None,
                           timeout=QUIESCE_PROBE_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {QUIESCE_PROBE_MS}")
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")   # quiescence proof + the atomicity lock
        except sqlite3.OperationalError as e:
            raise PreconditionError(
                f"skein.service not quiesced: the threads db is write-locked "
                f"({e}) — stop skein.service before migrating (§4.4)") from e
        # We hold the write lock now; no other writer can commit until we do.
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        try:
            # 1. Preconditions (fail-closed) under the lock — I1, no NULL thread_id,
            #    no prior backup, AND the full plan (which itself refuses a re-anchor
            #    collision) — ALL before the backup, so EVERY refusal leaves the db
            #    byte-untouched with no restore point buried (the plan is read-only, so
            #    running it before the backup is safe and the later backup, still under
            #    the held lock, is an identical pre-swap image).
            slugs, slug_to_genesis, _ = _ref_maps(conn)   # asserts I1
            n_null = _null_thread_id_count(conn)
            if n_null:
                raise PreconditionError(
                    f"{n_null} thread row(s) with a NULL thread_id — threads_new."
                    f"thread_id is NOT NULL, so these would be silently lost on copy; "
                    f"resolve by hand before migrating")
            prior = sorted(db_path.parent.glob(f"{db_path.name}.bak-threads-pk-*"))
            if prior:
                raise PreconditionError(
                    f"prior threads-pk backup exists ({prior[0].name}) — already "
                    f"migrated or a prior attempt touched this db; resolve by hand "
                    f"(the first .bak is the true pre-swap image) before re-running")
            survivors, result = _plan(conn, slugs, slug_to_genesis,
                                      _versions_set(conn))   # may raise (collision)

            # 2. Backup (online-backup API, WAL-consistent) — the restore point.
            #    Taken UNDER the held write lock, so it is a faithful pre-swap image;
            #    reached only after every fail-closed refusal above has passed.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = db_path.with_name(f"{db_path.name}.bak-threads-pk-{stamp}")
            _backup_copy(db_path, backup)

            # 3. The rewrite — build / copy / drop / rename / recreate-indexes — all
            #    still under the one lock. A plain INSERT (not OR IGNORE): _plan already
            #    collapsed byte-dups and refused collisions, so the survivors are unique
            #    — a plain INSERT surfaces any residual anomaly (a leftover dup, a NULL)
            #    LOUDLY as a rollback, never a silent drop.
            conn.execute(_THREADS_NEW_DDL)                 # table only, no indexes yet
            conn.executemany(
                "INSERT INTO threads_new "
                "(thread_hash, thread_id, from_id, to_id, type, content, weaver, "
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                survivors)
            conn.execute("DROP TABLE threads")
            conn.execute("ALTER TABLE threads_new RENAME TO threads")
            for ddl in _THREAD_INDEXES:                    # names now free
                conn.execute(ddl)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        # 4. Checkpoint post-commit, outside the write transaction (§4.4 step 6).
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass   # a non-truncated WAL is harmless; the swap already committed
    finally:
        conn.close()
    return result


# ── CLI (single db or --all; dry-run copy-proof first, then --live) ───────────

def _db_paths_from_registry() -> list:
    """Every registered project's skein.db (reuses the 3a resolver)."""
    from skein.migrations.migrate_threads_control import _db_paths_from_registry as f
    return f()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Phase 3b: swap threads.thread_hash to the PK + re-anchor Class B.")
    p.add_argument("db_path", type=Path, nargs="?", help="a single skein.db")
    p.add_argument("--all", action="store_true", help="every registered project")
    p.add_argument("--dry-run", action="store_true",
                   help="read-only; plan + counts only, mutate nothing (default)")
    p.add_argument("--live", action="store_true",
                   help="rewrite the LIVE db(s) (requires --i-understand-this-writes)")
    p.add_argument("--i-understand-this-writes", action="store_true",
                   help="explicit confirmation gate for --live")
    args = p.parse_args()

    if args.live and args.dry_run:
        p.error("give one of --dry-run / --live")
    if bool(args.all) == bool(args.db_path):
        p.error("give exactly one of: a db_path, or --all")
    live = bool(args.live)
    if live and not args.i_understand_this_writes:
        p.error("--live requires --i-understand-this-writes (this rewrites the hot "
                "threads table; quiesce skein.service first)")
    dry_run = not live   # default and --dry-run are both the safe read-only plan

    targets = (_db_paths_from_registry() if args.all
               else [(str(args.db_path), args.db_path)])
    if not targets:
        print("no databases found")
        return 0

    mode = "LIVE — REWRITING REAL DBS" if live else "dry-run (plan only, no writes)"
    print(f"[{mode}] thread_hash PK swap over {len(targets)} db(s)")
    failures = []
    for label, db in targets:
        try:
            r = migrate_db(db, dry_run=dry_run)
            state = ("already migrated (no-op)" if r.already_migrated
                     else f"reanchored={r.reanchored} collapsed={len(r.collapsed)} "
                          f"orphans={len(r.orphans)}")
            print(f"  {'PLANNED' if dry_run else 'OK'} {label}: {state}")
        except PreconditionError as e:
            print(f"  REFUSED {label}: {e}")
            failures.append(label)
        except Exception as e:  # noqa: BLE001 — isolate one bad db, keep the batch
            print(f"  FAILED {label}: {type(e).__name__}: {e}")
            failures.append(label)

    if failures:
        print(f"\n{len(failures)} db(s) refused/failed: {failures}")
        return 1
    print("\nDone. Run verify_threads_pk.py as the structural gate before trusting a "
          "live run." if dry_run else "\nDone. Run verify_threads_pk.py to confirm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
