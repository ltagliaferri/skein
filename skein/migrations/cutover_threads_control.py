#!/usr/bin/env python3
"""Phase 3a A3 cutover driver — the supervised live-run wrapper (design §5.A3 §7).

Thin orchestration over two already-felled primitives:
  - migrate_threads_control.migrate_db  (the re-anchor migration)
  - verify_threads_control.verify_db    (the differential oracle, full Leg A/B/C gate)

Per registered db it runs the copy-proof shape end-to-end and DIGEST-BINDS it, so
each db's migration is proven against its own pre-image before the run moves on:

  1. snapshot the pre-image  (SQLite online-backup API — torn-read-safe, incl WAL)
  2. digest the pre-image    (binds the proof to these exact bytes)
  3. migrate                 (the working target)
  4. verify(target, pre_snapshot=snapshot, expect_digest=digest) -> must be "verified"
     (a "not migrated (pre-A1 schema)" db with no control is accepted as a no-op)
  5. on ANY divergence -> ABORT the whole run (don't touch remaining dbs)

Two modes:
  --dry-run (default): the target is a COPY; the live db is NEVER written. This is
    the copy-proof — run it in the morning FIRST, on the quiesced ecosystem, to
    re-prove on the current bytes and catch any overnight drift.
  --live: the target IS the live db; the pre-image snapshot doubles as the restore
    point (``<db>.bak-threads-control-<stamp>``). REQUIRES --i-understand-this-writes.
    Quiesce first (stop skein.service) so no writer interleaves.

Restore (if a live db must be rolled back): stop skein.service, then
``cp <db>.bak-threads-control-<stamp> <db>`` (and delete <db>-wal/<db>-shm), start.

Usage:
    # morning, server stopped: re-prove on copies (non-destructive)
    python -m skein.migrations.cutover_threads_control --dry-run
    # then the real thing (server stopped), smallest-first, speakbot last:
    python -m skein.migrations.cutover_threads_control --live --i-understand-this-writes
    # a single db (either mode):
    python -m skein.migrations.cutover_threads_control --dry-run /path/to/skein.db
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skein.migrations import verify_threads_control as vtc  # noqa: E402
from skein.migrations.migrate_threads_control import (  # noqa: E402
    PreconditionError,
    _db_paths_from_registry,
    migrate_db,
)


def _backup_copy(src: Path, dst: Path) -> None:
    """Consistent snapshot src -> dst via the online-backup API (includes WAL)."""
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


def cutover_one(pid: str, db: Path, *, live: bool, stamp: str) -> Tuple[bool, str, list]:
    """Run migrate + full-gate verify for one db. Returns (passed, label, problems).
    A db PASSES only if the oracle returns "verified" (full Leg A/B/C, digest-bound)
    or "not migrated (pre-A1 schema)" (a no-op db) — never "incomplete"/"diverged".
    dry-run: target is a private temp COPY (live db untouched).
    live:    target is the live db; the pre-image snapshot is the restore point."""
    if live:
        pre = db.with_name(f"{db.name}.bak-threads-control-{stamp}")
        if pre.exists():
            return (False, "skipped (backup exists — already cut over?)",
                    ["a backup for this stamp already exists"])
        _backup_copy(db, pre)          # restore point == pre-image
        target = db
        tmp = None
    else:
        tmp = Path(tempfile.mkdtemp(prefix="cutover_dry_"))
        pre = tmp / "pre.db"
        target = tmp / "post.db"
        _backup_copy(db, pre)
        _backup_copy(db, target)
    try:
        with vtc._ro(pre) as c:
            digest = vtc.preimage_digest(c)   # digest the pre-image we bind to
        try:
            r = migrate_db(target)
        except PreconditionError as e:
            return (False, f"REFUSED: {e}", ["precondition refused"])
        status, problems, _ = vtc.verify_db(
            target, pre_snapshot=pre, expect_digest=digest)
        drops = {}
        for d in r.dropped:
            drops[d["category"]] = drops.get(d["category"], 0) + 1
        label = (f"{status} | rekeyed={r.rekeyed} pass={r.passed_through} "
                 f"dropped={len(r.dropped)}{(' ' + str(drops)) if drops else ''} "
                 f"refs={r.refs_rebuilt}")
        passed = status == "verified" or status.startswith("not migrated")
        # Surface a reason even when verify_db reported no explicit problems (e.g.
        # an "incomplete" gate) so a non-pass never slips through as OK.
        if not passed and not problems:
            problems = [f"gate not satisfied: status={status!r}"]
        return (passed, label, problems)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 3a A3 cutover driver.")
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
        p.error("--live requires --i-understand-this-writes (this rewrites real "
                "thread data; quiesce skein.service first)")
    if not live and not args.dry_run:
        args.dry_run = True  # default is the safe copy-proof

    targets = ([("(single)", args.db_path)] if args.db_path
               else _order_targets(_db_paths_from_registry()))
    if not targets:
        print("no databases found")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode = "LIVE — WRITING REAL DBS" if live else "dry-run (copies only)"
    print(f"[{mode}] A3 cutover over {len(targets)} db(s), smallest-first\n")
    ok = 0
    for pid, db in targets:
        try:
            passed, label, problems = cutover_one(pid, db, live=live, stamp=stamp)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR    {pid}: {type(e).__name__}: {e}")
            print(f"\nABORTED at {pid}. {ok} db(s) done. "
                  f"{'Restore from the .bak if needed.' if live else ''}")
            return 1
        if not passed:
            print(f"  DIVERGED {pid}: {label}")
            for pr in problems[:12]:
                print(f"             - {pr}")
            print(f"\nABORTED at {pid} — a non-pass is a BLOCKER. {ok} db(s) done."
                  + (f" Restore this db from {db.name}.bak-threads-control-{stamp} "
                     "if needed." if live else ""))
            return 1
        ok += 1
        print(f"  OK       {pid}: {label}")
    print(f"\nAll {ok} db(s) {'migrated + verified (LIVE)' if live else 'verified on copies'}. "
          + ("Start skein.service (it comes back on master = A2 writers live)."
             if live else "Safe to proceed to --live when quiesced."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
