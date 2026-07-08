#!/usr/bin/env python3
"""Phase 3a A3 migration — re-anchor control threads onto lineage genesis hashes.

The one Phase 3a step that rewrites busy thread data. It turns the legacy
slug-keyed control taxonomy (``status`` / ``assignment`` / ``archive`` threads
anchored on a folio's random slug) into the genesis-keyed shape the A4
genesis-only reader will require: control edges anchored on the lineage's
immutable genesis hash (``refs.genesis_hash``). See ``docs/PHASE_3_DESIGN.md``
§5.A3 and the pinned contract in ``finding-20260701-jwy7``.

By A3 run-time the input is a MIXED keyspace: the A2 writers have emitted
genesis-keyed control since deploy, so any status/assignment/archive set in the
A2->A3 window is already in target shape. So each control row is classified by
its ENDPOINT SHAPE first (design §5.A3), never blindly slug-resolved:

  - already genesis-keyed (the control anchor is a live ``refs.genesis_hash``):
    PASS THROUGH untouched — it is the target shape. Re-resolving it as a slug
    would fail to find it and wrongly drop a current status.
  - a genesis-shaped anchor (``sha256::``) matching NO live genesis: a
    ``genesis_orphan`` (its lineage was deleted after A2 wrote the row) — dropped
    but logged, distinct category from a slug orphan.
  - slug-keyed (legacy): resolve the folio slug against ``refs``:
      * unresolvable (deleted folio)         -> drop-but-logged (``slug_orphan``)
      * resolvable status/archive            -> re-key to a genesis self-loop
        ``from=to=genesis``; a non-self-loop (``from=agent``) preserves the
        setting agent in ``weaver`` (24bk #2), the agent moved off ``from``.
      * resolvable assignment                -> re-key ``from=genesis``, leave
        ``to`` (the assignee) untouched.

Then, in order (design §5.A3):
  1. backfill ``thread_hash`` for EVERY surviving row (every type), over its
     POST-transform canonical fields, into the non-unique A1 column.
  2. rebuild ``refs.status / assigned_to / archived`` from the re-anchored
     threads via the deterministic ``(created_at, thread_id)`` reduction — the
     SAME order the A1 reader uses — correcting any stale cache.

No ``UNIQUE`` index and no byte-duplicate collapse in 3a (§4): the re-anchor can
manufacture byte-identical control rows, but with no uniqueness enforced they are
harmless — step 2 picks one value and identical duplicates carry the same value.
The whole-table collapse + the ``UNIQUE``/PK swap are 3b.

Preconditions (the db is REFUSED with ``PreconditionError``, before any mutation):
  - I1: ``genesis_hash`` unique per lineage (no two refs share a genesis). The
    migration asserts it, never assumes it.
  - cache-only control: a folio whose ``refs`` cache carries non-default control
    (status != 'open', assigned_to set, or archived truthy) with NO covering
    control thread of that type (slug- OR genesis-anchored) would be silently
    cleared by the rebuild. Refuse rather than lose the state (empirically 0 today
    per finding-20260630-eq68, but enforced not assumed).

Idempotent: a second run finds every control row already genesis-keyed (pass
through), recomputes the identical ``thread_hash``, rebuilds identical ``refs``,
and drops nothing.

Copy-proof (design §3, §7): prove on a COPY of every real db first (run this,
then ``verify_threads_control.py`` as the gate), THEN run live ``--all --backup``
with a full pre-migration archive in hand. The live run rewrites real data —
quiesce the busiest station (speakbot) first.

Usage:
    python -m skein.migrations.migrate_threads_control DB
    python -m skein.migrations.migrate_threads_control DB --dry-run
    python -m skein.migrations.migrate_threads_control --all --backup
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# Make the repo root importable so a direct
# ``python skein/migrations/migrate_threads_control.py`` (not just ``-m``) works.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skein.identity import compute_thread_hash  # noqa: E402
from skein.storage import load_project_registry  # noqa: E402

BUSY_TIMEOUT_MS = 30000

# Control types and the endpoint column that anchors each to its folio (design
# §5.A1, identical to the A1 reader and the oracle): status/archive are self-loops
# keyed on to_id; assignment is from=folio (to is the assignee, left untouched).
CONTROL_ANCHORS: Tuple[Tuple[str, str], ...] = (
    ("status", "to_id"),
    ("archive", "to_id"),
    ("assignment", "from_id"),
)
_HASH_PREFIX = "sha256::"
ARCHIVED_MARKER = "archived"


class PreconditionError(Exception):
    """A3 refuses a db that violates a precondition (I1 collision, or cache-only
    control with no covering thread) — a TYPED refusal, so a caller can assert the
    refusal without an incidental crash satisfying the same check."""


def _assert_pre_contraction_schema(conn) -> None:
    """Typed refusal when the db has no refs control columns: this SPENT
    migration reads AND rebuilds refs.status/assigned_to/archived, which the
    threads-only contraction (2026-07-08) dropped. Checked on the SAME connection
    the work runs on — for the live path that means UNDER the held BEGIN
    IMMEDIATE lock, so a concurrent drop_refs_control cannot land between the
    check and the work (deep_code_audit r4 finding 5); an earlier separate-probe
    version had exactly that race. Pre-contraction dbs/backups always carry the
    columns and pass untouched."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(refs)")}
    if "status" not in cols:
        raise PreconditionError(
            "refs has no control columns — post-contraction db (threads-only "
            "contraction, 2026-07-08); the A3 migration is inapplicable to it")


@dataclass
class MigrationResult:
    """Observable outcome of one ``migrate_db`` run."""
    dropped: List[dict] = field(default_factory=list)   # {thread_id, category, type}
    rekeyed: int = 0                # control rows re-anchored slug -> genesis
    passed_through: int = 0         # control rows already genesis-keyed
    thread_hash_backfilled: int = 0  # surviving rows stamped (all types)
    refs_rebuilt: int = 0           # refs rows whose control cache was rewritten
    dry_run: bool = False


def _is_genesis_shaped(anchor: Optional[str]) -> bool:
    """True if ``anchor`` looks like a content-hash address (``sha256::<hex>``),
    so an orphan whose lineage was deleted is logged as a genesis orphan rather
    than a slug orphan. Slugs (``folio-...`` / ``status-...-folio``) never carry
    this prefix, so the two orphan classes never conflate."""
    return isinstance(anchor, str) and anchor.startswith(_HASH_PREFIX)


def _ref_maps(conn: sqlite3.Connection) -> Tuple[set, Dict[str, str], Dict[str, str]]:
    """(live slugs, slug->genesis, genesis->slug). Asserts I1 (genesis unique per
    lineage) — a violation is a PreconditionError, the migration never assumes it."""
    slugs: set = set()
    slug_to_genesis: Dict[str, str] = {}
    genesis_to_slug: Dict[str, str] = {}
    for slug, genesis in conn.execute("SELECT slug, genesis_hash FROM refs"):
        slugs.add(slug)
        slug_to_genesis[slug] = genesis
        if genesis is not None:
            if genesis in genesis_to_slug and genesis_to_slug[genesis] != slug:
                raise PreconditionError(
                    f"I1 violation: genesis_hash {genesis!r} shared by lineages "
                    f"{genesis_to_slug[genesis]!r} and {slug!r}")
            genesis_to_slug[genesis] = slug
    return slugs, slug_to_genesis, genesis_to_slug


def _assert_no_cache_only_control(
    conn: sqlite3.Connection, slugs: set, genesis_to_slug: Dict[str, str]
) -> None:
    """Refuse a db whose refs cache carries non-default control with no covering
    thread (design §1 A3 precondition, Fell finding 5) — the rebuild would clear it
    silently. Coverage is computed over the ORIGINAL (pre-transform) threads,
    anchored by slug OR genesis (the input is a mixed keyspace)."""
    covered: Dict[str, set] = {ttype: set() for ttype, _ in CONTROL_ANCHORS}
    for ttype, anchor_col in CONTROL_ANCHORS:
        for (a,) in conn.execute(
            f"SELECT {anchor_col} FROM threads WHERE type = ?", (ttype,)
        ):
            if a in slugs:
                covered[ttype].add(a)
            elif a in genesis_to_slug:
                covered[ttype].add(genesis_to_slug[a])
    for r in conn.execute(
        "SELECT slug, status, assigned_to, archived FROM refs"
    ):
        slug = r["slug"]
        if r["status"] not in (None, "open") and slug not in covered["status"]:
            raise PreconditionError(
                f"cache-only control {slug}.status={r['status']!r}: non-default "
                f"but no covering status thread (rebuild would clear it)")
        if r["assigned_to"] is not None and slug not in covered["assignment"]:
            raise PreconditionError(
                f"cache-only control {slug}.assigned_to={r['assigned_to']!r}: set "
                f"but no covering assignment thread (rebuild would clear it)")
        if r["archived"] and slug not in covered["archive"]:
            raise PreconditionError(
                f"cache-only control {slug}.archived: truthy but no covering "
                f"archive thread (rebuild would clear it)")


def _plan_row(
    row: sqlite3.Row, slugs: set, slug_to_genesis: Dict[str, str],
    genesis_to_slug: Dict[str, str],
) -> Tuple[Optional[tuple], Optional[dict], str]:
    """Classify one thread row. Returns (update, drop, kind) where exactly one of
    update/drop is set:
      update = (thread_id, from_id, to_id, weaver, thread_hash) for a survivor
      drop   = {thread_id, category, type} for an orphan
      kind   = 'rekeyed' | 'passthrough' | 'noncontrol' | 'drop'
    """
    tid = row["thread_id"]
    ttype = row["type"]
    from_id, to_id, weaver = row["from_id"], row["to_id"], row["weaver"]
    content, created_at = row["content"], row["created_at"]

    # Resolve the anchor SLUG-FIRST, then genesis, matching the A1 tolerant reader
    # (its anchor_map is pref=0 slug / pref=1 genesis) and the Leg C answer key
    # (``if anchor in slugs ... elif anchor in genesis_to_slug``). The reader was
    # hardened in A1 to be single-valued slug-first "regardless of I1 state"; the
    # rebuild MUST use the same precedence (design §5.A1/§8) so a slug that also
    # collided with another lineage's genesis can never route to a different folio
    # here than the reader/oracle expect. (Slug and genesis namespaces are in fact
    # disjoint — slugs are ``type-date-xxxx``, genesis is ``sha256::…`` — so the two
    # branches are mutually exclusive on real data; matching precedence is the
    # consistency guarantee, not a behaviour change.)
    kind = "noncontrol"
    if ttype in ("status", "archive"):
        anchor = to_id                       # self-loop anchor (A1 reader's column)
        if anchor in slugs:                  # legacy slug
            genesis = slug_to_genesis[anchor]
        elif anchor in genesis_to_slug:      # already genesis-keyed
            genesis = anchor
        else:                                # orphan (deleted lineage)
            cat = "genesis_orphan" if _is_genesis_shaped(anchor) else "slug_orphan"
            return None, {"thread_id": tid, "category": cat, "type": ttype}, "drop"
        # Target shape is a genesis SELF-LOOP regardless of prior keying — A3 must
        # produce it for every kept row, not assume already-genesis == already a
        # self-loop. A genesis-keyed non-self-loop (from=agent, to=genesis) is
        # producible via POST /threads and would otherwise pass through and fail
        # the oracle's I2 self-loop check, blocking the A4 flip. A non-self-loop
        # moves the setting agent off `from` into weaver (24bk #2); a row already
        # in target shape (from==to==genesis) is a true passthrough (no change).
        if from_id == genesis and to_id == genesis:
            kind = "passthrough"
        else:
            if weaver is None and from_id != to_id:
                weaver = from_id
            from_id = to_id = genesis
            kind = "rekeyed"
    elif ttype == "assignment":
        anchor = from_id                     # from = folio
        if anchor in slugs:                  # legacy slug -> re-key from only
            from_id = slug_to_genesis[anchor]  # to (the assignee) untouched
            kind = "rekeyed"
        elif anchor in genesis_to_slug:      # already genesis-keyed -> pass through
            kind = "passthrough"
        else:
            cat = "genesis_orphan" if _is_genesis_shaped(anchor) else "slug_orphan"
            return None, {"thread_id": tid, "category": cat, "type": ttype}, "drop"

    # Every survivor (control re-keyed/passed, and every non-control row) is
    # content-addressed over its POST-transform canonical fields.
    thread_hash = compute_thread_hash(
        from_id, to_id, ttype, weaver, created_at, content)
    return (tid, from_id, to_id, weaver, thread_hash), None, kind


def _reduce_control(
    conn: sqlite3.Connection, ttype: str, anchor_col: str,
    value_fn: Callable[[sqlite3.Row], object], genesis_to_slug: Dict[str, str],
) -> Dict[str, object]:
    """Reduce POST-transform (genesis-keyed) control threads of ``ttype`` to one
    value per folio slug, latest by ``(created_at, thread_id)`` — the deterministic
    order the A1 reader uses. Orphans (should be none post-drop) are skipped."""
    best_key: Dict[str, tuple] = {}
    best_val: Dict[str, object] = {}
    cur = conn.execute(
        "SELECT thread_id, from_id, to_id, content, created_at "
        "FROM threads WHERE type = ?", (ttype,))
    for row in cur.fetchall():
        anchor = row[anchor_col]
        slug = genesis_to_slug.get(anchor)
        if slug is None:
            continue
        key = (row["created_at"], row["thread_id"])
        if slug not in best_key or key > best_key[slug]:
            best_key[slug] = key
            best_val[slug] = value_fn(row)
    return best_val


def _rebuild_refs(
    conn: sqlite3.Connection, slug_to_genesis: Dict[str, str],
    genesis_to_slug: Dict[str, str],
) -> int:
    """Rebuild refs.status/assigned_to/archived from the re-anchored threads.
    Returns the number of refs rows written. Every live folio is set to its
    reduced value (or the default when no control thread covers it), correcting
    any stale cache."""
    status = _reduce_control(
        conn, "status", "to_id", lambda r: r["content"], genesis_to_slug)
    assigned = _reduce_control(
        conn, "assignment", "from_id", lambda r: r["to_id"], genesis_to_slug)
    archived = _reduce_control(
        conn, "archive", "to_id",
        lambda r: 1 if r["content"] == ARCHIVED_MARKER else 0, genesis_to_slug)

    written = 0
    for slug in slug_to_genesis:
        status_val = status.get(slug)
        # Mirror the reducer's value contract exactly: a present-None (or absent)
        # status coerces to the 'open' default; a present '' is preserved.
        status_col = "open" if status_val is None else status_val
        assigned_col = assigned.get(slug)          # None default
        archived_col = 1 if archived.get(slug) else 0
        conn.execute(
            "UPDATE refs SET status = ?, assigned_to = ?, archived = ? "
            "WHERE slug = ?",
            (status_col, assigned_col, archived_col, slug))
        written += 1
    return written


def migrate_db(db_path, *, dry_run: bool = False) -> MigrationResult:
    """Re-anchor + thread_hash backfill + refs rebuild for one db, in one
    transaction. Raises ``PreconditionError`` (I1 collision / cache-only control)
    before any mutation. Idempotent. See the module docstring for the transform.

    ``dry_run`` is strictly read-only: it classifies and returns the plan
    (``dropped`` + counts) without ALTERing, updating, or rebuilding anything —
    safe on a live WAL db and on a pre-A1 copy whose thread_hash column is absent.
    """
    db_path = Path(db_path)
    result = MigrationResult(dry_run=dry_run)

    if dry_run:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            _assert_pre_contraction_schema(conn)
            slugs, slug_to_genesis, genesis_to_slug = _ref_maps(conn)
            _assert_no_cache_only_control(conn, slugs, genesis_to_slug)
            for row in conn.execute(
                "SELECT thread_id, from_id, to_id, type, content, weaver, "
                "created_at FROM threads"
            ).fetchall():
                update, drop, kind = _plan_row(
                    row, slugs, slug_to_genesis, genesis_to_slug)
                if drop is not None:
                    result.dropped.append(drop)
                    continue
                result.thread_hash_backfilled += 1  # every survivor is stamped
                if kind == "rekeyed":
                    result.rekeyed += 1
                elif kind == "passthrough":
                    result.passed_through += 1
            result.refs_rebuilt = len(slug_to_genesis)  # every folio is rebuilt
            return result
        finally:
            conn.close()

    # EVERYTHING — schema-ensure, preconditions, transform, rebuild — runs inside
    # ONE BEGIN IMMEDIATE transaction, so it ALL rolls back together on any failure
    # (design §5.A3 atomicity: no partial write escapes). A refused db is left
    # byte-untouched because the refusal rolls back even the additive thread_hash
    # ALTER; and a mid-transform write failure can never leave the schema half-
    # migrated. isolation_level=None -> autocommit; we drive BEGIN/COMMIT ourselves
    # so the write lock is held across read+compute+write (no interleaving writer).
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _assert_pre_contraction_schema(conn)  # under the held write lock
            slugs, slug_to_genesis, genesis_to_slug = _ref_maps(conn)  # asserts I1
            _assert_no_cache_only_control(conn, slugs, genesis_to_slug)

            # Ensure the A1 thread_hash column exists — INSIDE the transaction, so a
            # pre-A1 db half-migrated by a later failure never keeps an escaped
            # column (SQLite DDL is transactional). A live post-A1 db already has
            # it -> no-op. This is the one column A3 writes.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
            if "thread_hash" not in cols:
                conn.execute("ALTER TABLE threads ADD COLUMN thread_hash TEXT")

            updates: List[tuple] = []
            drops: List[str] = []
            for row in conn.execute(
                "SELECT thread_id, from_id, to_id, type, content, weaver, "
                "created_at FROM threads"
            ).fetchall():
                update, drop, kind = _plan_row(
                    row, slugs, slug_to_genesis, genesis_to_slug)
                if drop is not None:
                    drops.append(drop["thread_id"])
                    result.dropped.append(drop)
                    continue
                updates.append(update)
                if kind == "rekeyed":
                    result.rekeyed += 1
                elif kind == "passthrough":
                    result.passed_through += 1

            if drops:
                conn.executemany(
                    "DELETE FROM threads WHERE thread_id = ?",
                    [(tid,) for tid in drops])
            if updates:
                conn.executemany(
                    "UPDATE threads SET from_id = ?, to_id = ?, weaver = ?, "
                    "thread_hash = ? WHERE thread_id = ?",
                    [(nf, nt, nw, th, tid)
                     for (tid, nf, nt, nw, th) in updates])
            result.thread_hash_backfilled = len(updates)

            result.refs_rebuilt = _rebuild_refs(
                conn, slug_to_genesis, genesis_to_slug)

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return result


# ── CLI (copy-proof first, then live --all --backup) ─────────────────────────

def slug_keyed_control_remaining(db_path) -> int:
    """Count control threads still anchored on a LIVE folio SLUG — the observable the
    A4 gate reads. FOLIO control ONLY (A2 carry-forward, finding-20260701-ehjw): an
    anchor that is not a live ``refs.slug`` — a non-folio id (a ``status`` posted on a
    thread-id/agent-id), or a deleted-folio orphan — is pass-through / dropped by the
    tolerant reader and must NOT block the genesis-only flip. Genesis-keyed control
    (anchor == ``refs.genesis_hash``) is already migrated and does not count. 0 means
    the db holds no slug-keyed folio control, i.e. the genesis-only reader would read
    it identically to the A1 tolerant reader. Read-only (``mode=ro``), safe on a live
    WAL db. ``anchor_col`` is a fixed internal literal (CONTROL_ANCHORS), not input.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        live_slugs = {r["slug"] for r in conn.execute("SELECT slug FROM refs")}
        remaining = 0
        for ttype, anchor_col in CONTROL_ANCHORS:
            for (anchor,) in conn.execute(
                f"SELECT {anchor_col} FROM threads WHERE type = ?", (ttype,)
            ):
                if anchor in live_slugs:
                    remaining += 1
        return remaining
    finally:
        conn.close()


def all_dbs_migrated(db_paths) -> bool:
    """The A4 gate: True iff EVERY db in ``db_paths`` holds 0 slug-keyed folio control
    (so the genesis-only reader flip is safe ecosystem-wide). One un-migrated db
    blocks the whole flip — under the genesis-only reader its non-open folios would
    read 'open' (``folio -> genesis -> to_id = genesis`` finds nothing; design §5.A4).
    Empty input is vacuously True.
    """
    return all(slug_keyed_control_remaining(p) == 0 for p in db_paths)


def _backup_db(db_path: Path) -> Path:
    """A consistent single-file backup via SQLite's online-backup API."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.bak-threads-control-{stamp}")
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


def _print_result(label: str, r: MigrationResult) -> None:
    verb = "would re-key" if r.dry_run else "re-keyed"
    by_cat: Dict[str, int] = {}
    for d in r.dropped:
        by_cat[d["category"]] = by_cat.get(d["category"], 0) + 1
    drops = ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items())) or "none"
    print(f"  {label}: {verb} {r.rekeyed} | passed-through {r.passed_through} | "
          f"dropped {len(r.dropped)} ({drops}) | "
          f"thread_hash {r.thread_hash_backfilled} | refs {r.refs_rebuilt}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Phase 3a A3: re-anchor control threads onto genesis hashes.")
    p.add_argument("db_path", type=Path, nargs="?", help="A single skein.db")
    p.add_argument("--all", action="store_true", help="Every registered project")
    p.add_argument("--dry-run", action="store_true", help="Read-only; plan only")
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
    print(f"[{mode}] re-anchor control threads over {len(targets)} db(s)")
    failures = []
    for label, db in targets:
        try:
            if not args.dry_run and args.backup:
                backup = _backup_db(db)
                print(f"  backed up {label} -> {backup.name}")
            r = migrate_db(db, dry_run=args.dry_run)
            _print_result(label, r)
        except PreconditionError as e:
            print(f"  REFUSED {label}: {e}")
            failures.append(label)
        except Exception as e:  # isolate one bad db; keep the batch going
            print(f"  FAILED {label}: {type(e).__name__}: {e}")
            failures.append(label)

    if failures:
        print(f"\n{len(failures)} db(s) refused/failed: {failures}")
        sys.exit(1)
    print("\nDone. Run verify_threads_control.py as the equivalence gate before "
          "trusting a live run.")


if __name__ == "__main__":
    main()
