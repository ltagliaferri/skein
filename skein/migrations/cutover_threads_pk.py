#!/usr/bin/env python3
"""Phase 3b cutover driver — the supervised live-run wrapper for the thread_hash
PK swap (design §4.4 / §8 / §9).

Thin orchestration over two already-felled primitives, plus the differential
equivalence legs the structural verifier cannot do alone because they need the
PRE-image:

  - threads_pk_swap.migrate_db   the PK-swap + Class B re-anchor. Makes its OWN
      WAL-consistent ``<db>.bak-threads-pk-<stamp>`` restore point UNDER the held
      write lock, and refuses if a prior one exists (§4.4). The driver therefore
      NEVER writes a ``.bak-threads-pk`` itself — its pre-image snapshot is a
      throwaway temp file, so migrate_db still owns the one true restore point.
  - verify_threads_pk.verify_db  the read-only STRUCTURAL gate (§8): thread_hash
      is the UNIQUE PK, every row self-verifies, no NULL required column, every
      Class B / version endpoint resolves, all six idx_threads_* present, I1 holds.

  Differential equivalence legs (need the pre-image; design §8):
  - CONTROL read unchanged — get_latest_statuses/assignments/archives per folio,
    before vs after. Reuses the felled 3a oracle's reader + diff (identical
    normalization), so control cannot regress silently across the swap.
  - Class B DISPLAY unchanged — get_threads_display() (behind GET /threads and
    /search) presents the SAME set of visible edges (slugs for the genesis-anchored
    Class B types) before vs after the re-anchor. Compared as a SET of visible
    rows (thread_id excluded): a byte-dup collapse removes a duplicate ROW but no
    visible EDGE, so set-equality is the collapse-invariant statement of "the API
    reads identically"; the quantitative loss is caught by the row-count leg.
  - ROW-COUNT reconciliation — post rowcount == pre rowcount − len(collapsed). The
    ONLY accepted loss is the logged byte-duplicate collapse (§2.1 / §4.1 / §8); a
    re-anchor collision raises inside migrate_db and never reaches here.

Per db, in order (smallest db first to build confidence, speakbot LAST):
  1. snapshot the pre-image to a TEMP file (torn-read-safe, incl. WAL)
  2. digest-bind it (a full-threads + refs digest; re-asserted after the legs to
     prove the pre-image the legs compared against was itself never mutated)
  3. capture the pre-state control map / display set / row count (over materialized
     copies, so the pre-image stays byte-pristine for the digest re-assert)
  4. migrate_db(target)
  5. structural verify_db(target) — must be clean (zero problems)
  6. the three differential legs against the pre-state
  7. ANY divergence -> ABORT the whole run (leave the remaining dbs untouched)

Two modes:
  --dry-run (default): the target is a private temp COPY; the live db is NEVER
    written. The copy-proof — run it FIRST on the quiesced ecosystem to re-prove on
    the current bytes and catch any drift before writing anything.
  --live: the target IS the live db. migrate_db writes the ``.bak-threads-pk-<stamp>``
    restore point under its held write lock. REQUIRES --i-understand-this-writes AND
    a quiesced skein.service (migrate_db verifies quiescence via BEGIN IMMEDIATE and
    refuses otherwise).

Restore (roll back a live db): stop skein.service, then
    cp <db>.bak-threads-pk-<stamp> <db>   (and delete <db>-wal / <db>-shm), restart.

Usage:
    # quiet period, server stopped: re-prove on copies (non-destructive)
    python -m skein.migrations.cutover_threads_pk --dry-run
    # then the real thing (server stopped), smallest-first, speakbot last:
    python -m skein.migrations.cutover_threads_pk --live --i-understand-this-writes
    # a single db (either mode):
    python -m skein.migrations.cutover_threads_pk --dry-run /path/to/skein.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Set, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skein.migrations import verify_threads_control as vtc  # noqa: E402
from skein.migrations import verify_threads_pk as vpk  # noqa: E402
from skein.migrations.threads_pk_swap import (  # noqa: E402
    PreconditionError,
    _db_paths_from_registry,
    migrate_db,
)
from skein.storage import LogDatabase  # noqa: E402


def _backup_copy(src: Path, dst: Path) -> None:
    """Consistent snapshot src -> dst via the online-backup API (includes committed
    WAL frames), taken over a read-only source connection (never writes src)."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def _order_targets(targets: List[Tuple[str, Path]]) -> List[Tuple[str, Path]]:
    """Smallest db first (build confidence), largest (speakbot) last."""
    return sorted(targets, key=lambda t: t[1].stat().st_size)


def _threads_digest(db_path: Path) -> str:
    """A stable digest over the FULL threads table + refs of ``db_path`` — the
    pre-image binding (design §8 digest-bind). Read-only (mode=ro), order-independent
    (the row list is sorted), canonical JSON, sha256. Wider than the 3a control-only
    ``preimage_digest`` because 3b's legs read the whole table (control + Class B
    display), so the binding must cover every row the equivalence legs compare."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        threads = [
            [r["thread_id"], r["from_id"], r["to_id"], r["type"], r["content"],
             r["weaver"], str(r["created_at"]), r["thread_hash"]]
            for r in conn.execute(
                "SELECT thread_id, from_id, to_id, type, content, weaver, "
                "created_at, thread_hash FROM threads")
        ]
        refs = [
            [r["slug"], r["genesis_hash"]]
            for r in conn.execute("SELECT slug, genesis_hash FROM refs")
        ]
    finally:
        conn.close()
    threads.sort(key=lambda x: (str(x[0]), str(x[7])))
    refs.sort(key=lambda x: str(x[0]))
    blob = json.dumps({"threads": threads, "refs": refs},
                      sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _thread_count(db_path: Path) -> int:
    """Total thread rows (read-only)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    finally:
        conn.close()


def _display_set(db_path: Path) -> Set[tuple]:
    """The set of VISIBLE thread rows as get_threads_display presents them (behind
    GET /threads and /search), read over a materialized copy so ``db_path`` is never
    mutated (LogDatabase.__init__ writes). thread_id is excluded from the key: it is
    an audit handle, not a display field, and a byte-dup collapse legitimately drops
    one of two identical-on-every-visible-field rows — so a SET keyed on the visible
    fields is exactly the collapse-invariant 'the reader presents the same edges'
    statement (the quantitative delta is the row-count leg's job)."""
    with vtc._materialized(db_path) as copy:
        db = LogDatabase(str(copy))
        threads = db.get_threads_display()
    return {
        (t.from_id, t.to_id, t.type, t.content, t.weaver,
         t.created_at.isoformat() if t.created_at is not None else None)
        for t in threads
    }


def _control_map(db_path: Path) -> Dict[str, Dict[str, object]]:
    """Per-folio control read (status/assigned_to/archived), normalized identically
    to the felled 3a oracle. Read over a materialized copy (keeps db_path pristine)."""
    return vtc.compute_manifest(db_path)   # _materialized -> _a1_reads


def cutover_one(pid: str, db: Path, *, live: bool) -> Tuple[bool, str, List[str]]:
    """Migrate + structural-verify + the three differential legs for one db.
    Returns (passed, label, problems). PASSES only if the structural gate is clean
    AND all three differential legs are equivalent.

    dry-run: the target is a private temp COPY (the live db is untouched).
    live:    the target IS the live db; migrate_db writes the .bak restore point.
    Either way the pre-image the legs compare against is a THROWAWAY temp snapshot,
    so migrate_db keeps sole ownership of the ``.bak-threads-pk`` restore point."""
    tmp = Path(tempfile.mkdtemp(prefix="cutover_pk_"))
    try:
        pre = tmp / "pre.db"
        _backup_copy(db, pre)                 # pre-image (temp; NOT a .bak)
        pre_digest = _threads_digest(pre)     # bind the pre-image

        # Pre-state captured BEFORE any mutation, over materialized copies so `pre`
        # stays byte-pristine for the digest re-assert below.
        pre_ctl = _control_map(pre)
        pre_disp = _display_set(pre)
        pre_n = _thread_count(pre)

        if live:
            target = db                       # migrate_db writes db.bak-threads-pk-*
        else:
            target = tmp / "post.db"
            _backup_copy(db, target)          # the copy we migrate + prove

        try:
            r = migrate_db(target)
        except PreconditionError as e:
            return (False, f"REFUSED: {e}", ["precondition refused"])

        # The pre-image must be byte-unchanged — proves the legs below compared
        # against a faithful, un-mutated pre-image (defense against a read that
        # accidentally wrote through to `pre`).
        if _threads_digest(pre) != pre_digest:
            return (False, "pre-image digest changed after migrate — the bound "
                    "pre-image was mutated; the equivalence proof is void",
                    ["pre-image digest drift"])

        # Structural gate (§8) — zero problems required — then the three differential
        # legs against the pre-state we captured. An already-migrated db takes the
        # same full verification (its pre-image is post-swap on both sides, so the
        # legs are trivially equivalent); it is proven, never waved through.
        collapsed = len(r.collapsed)
        structural, warnings = vpk.verify_db(target)
        problems = list(structural)
        problems += _differential_legs_precomputed(
            pre_ctl, pre_disp, pre_n, target, collapsed)

        if r.already_migrated:
            label = "already migrated (no-op) — verified"
        else:
            drops = f" collapsed={collapsed}" if collapsed else ""
            warn = f" warnings={len(warnings)}" if warnings else ""
            label = f"reanchored={r.reanchored} orphans={len(r.orphans)}{drops}{warn}"
        return (not problems, label, problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _differential_legs_precomputed(
    pre_ctl: Dict[str, Dict[str, object]], pre_disp: Set[tuple], pre_n: int,
    post: Path, collapsed: int,
) -> List[str]:
    """The three legs using an already-captured pre-state (so `pre` is read once,
    before migrate, and never again for the legs). Post-state is read fresh from
    `post` after the swap."""
    problems: List[str] = []
    post_ctl = _control_map(post)
    problems += vtc._diff_maps("control", pre_ctl, post_ctl, "pre", "post")

    post_disp = _display_set(post)
    lost = pre_disp - post_disp
    gained = post_disp - pre_disp
    if lost:
        problems.append(
            f"display: {len(lost)} visible edge(s) present before but not after "
            f"(e.g. {sorted(str(x) for x in lost)[:3]})")
    if gained:
        problems.append(
            f"display: {len(gained)} visible edge(s) present after but not before "
            f"(e.g. {sorted(str(x) for x in gained)[:3]})")

    post_n = _thread_count(post)
    if post_n != pre_n - collapsed:
        problems.append(
            f"row-count: post={post_n} != pre({pre_n}) - collapsed({collapsed}) "
            f"= {pre_n - collapsed} — a loss beyond the logged byte-dup collapse")
    return problems


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 3b thread_hash-PK cutover driver.")
    p.add_argument("db_path", type=Path, nargs="?", help="a single skein.db")
    p.add_argument("--dry-run", action="store_true",
                   help="prove on COPIES; never write a live db (default)")
    p.add_argument("--live", action="store_true",
                   help="migrate the LIVE dbs (requires --i-understand-this-writes)")
    p.add_argument("--i-understand-this-writes", action="store_true",
                   help="explicit confirmation gate for --live")
    args = p.parse_args()

    if args.live and args.dry_run:
        p.error("give one of --dry-run / --live")
    live = bool(args.live)
    if live and not args.i_understand_this_writes:
        p.error("--live requires --i-understand-this-writes (this rewrites the hot "
                "threads table; quiesce skein.service first)")
    if not live and not args.dry_run:
        args.dry_run = True  # default is the safe copy-proof

    targets = ([("(single)", args.db_path)] if args.db_path
               else _order_targets(_db_paths_from_registry()))
    if not targets:
        print("no databases found")
        return 0

    mode = "LIVE — WRITING REAL DBS" if live else "dry-run (copies only)"
    print(f"[{mode}] 3b PK-swap cutover over {len(targets)} db(s), smallest-first\n")
    ok = 0
    for pid, db in targets:
        try:
            passed, label, problems = cutover_one(pid, db, live=live)
        except Exception as e:  # noqa: BLE001 — a crash mid-run is an abort
            print(f"  ERROR    {pid}: {type(e).__name__}: {e}")
            print(f"\nABORTED at {pid}. {ok} db(s) done."
                  + (" Restore any touched db from its .bak-threads-pk-* if needed."
                     if live else ""))
            return 1
        if not passed:
            print(f"  DIVERGED {pid}: {label}")
            for pr in problems[:12]:
                print(f"             - {pr}")
            if len(problems) > 12:
                print(f"             ... and {len(problems) - 12} more")
            print(f"\nABORTED at {pid} — a non-pass is a BLOCKER. {ok} db(s) done."
                  + (f" Restore this db from {db.name}.bak-threads-pk-* if needed."
                     if live else ""))
            return 1
        ok += 1
        print(f"  OK       {pid}: {label}")
    print(f"\nAll {ok} db(s) "
          + ("migrated + verified (LIVE). Restart skein.service on the OR IGNORE "
             "write-path code." if live
             else "verified on copies. Safe to proceed to --live when quiesced."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
