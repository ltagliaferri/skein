#!/usr/bin/env python3
# SPENT — executed 2026-07-03; all dbs folios-free, now an idempotent no-op.
# Kept as a record (with tests/test_drop_folios.py) of the destructive migration.
"""Phase 3a A5 Part 2 — the destructive live `folios` DROP driver (design §5.A5).

The final Phase-3a contraction: drop the legacy `folios` table on every registered
db. Reads have been off `folios` since the commit-C read-flip and A5 Part 1 retired
the writers, so on a db running Part-1 code `folios` is a frozen, non-authoritative
vestige — this reclaims its space and removes the `folios.status`/`folios.archived`
columns that could otherwise drift.

Modeled on cutover_threads_control.py (the A3 destructive-migration template). Per
registered db, fail-closed at every gate:

  1. precondition check (folios present + all data provably safe to discard)
  2. --live only: refuse if a prior folios-drop backup exists (don't bury the
     true restore point); snapshot `<db>.bak-folios-drop-<stamp>` (the restore point)
  3. DROP TABLE folios  (its indexes drop with it)
  4. verify: folios gone; versions/refs/threads row counts UNCHANGED; a head read
     still resolves — the drop removed ONLY folios and nothing else
  5. on ANY precondition/verify failure -> ABORT the whole run (don't touch the
     remaining dbs); in --live the already-dropped dbs are restorable from their .bak

PRECONDITION (design §5.A5 / 24bk — re-checked LIVE, fail-closed). A db passes iff
its `folios` is provably redundant with versions/refs/threads:
  - folios ⊆ refs           : every folios.folio_id has a refs.slug (no folio
                              orphaned from the authoritative head layer)
  - nonopen_no_thread == 0  : no non-open folios.status without a covering status
                              thread (nothing non-open lives ONLY in the cache)
  - folios.assigned_to      : all NULL (eq68: empty ecosystem-wide)
  - folios.archived         : all 0    (eq68: empty ecosystem-wide)
  - target_agent / omlet / acknowledged_at / metadata mirrored in refs (asserted,
    not assumed — valid because the check runs on a quiesced db still carrying the
    pre-Part-1 dual-write, where folios == refs)
A db with NO folios table (already dropped / born folios-free) is a no-op PASS.

Two modes (like the A3 cutover):
  --dry-run (default): the target is a COPY; the live db is NEVER written. The
    copy-proof — run it first, on the quiesced ecosystem, to re-prove on current
    bytes and catch overnight drift.
  --live: the target IS the live db; the pre-image snapshot doubles as the restore
    point. REQUIRES --i-understand-this-drops-folios. Quiesce skein.service first,
    and restart it on Part-1 code (which never touches folios) afterward.

Restore (roll back one live db): stop skein.service, then
`cp <db>.bak-folios-drop-<stamp> <db>` (and delete <db>-wal / <db>-shm), start.

Usage:
    # quiesced ecosystem: re-prove on copies (non-destructive)
    python -m skein.migrations.drop_folios --dry-run
    # the real thing (server stopped), smallest-first, speakbot last:
    python -m skein.migrations.drop_folios --live --i-understand-this-drops-folios
    # a single db (either mode):
    python -m skein.migrations.drop_folios --dry-run /path/to/skein.db
"""

from __future__ import annotations

import argparse
import os
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

from skein.identity import compute_folio_hash  # noqa: E402
from skein.migrations.migrate_threads_control import _db_paths_from_registry  # noqa: E402
from skein.storage import KNURL_AVAILABLE  # noqa: E402

_MIRROR_COLS = ("target_agent", "omlet", "acknowledged_at")


def _backup_copy(src: Path, dst: Path) -> None:
    """Consistent snapshot src -> dst via the online-backup API (torn-read-safe,
    includes committed WAL frames)."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def _norm_meta(v) -> str:
    """NULL / '' / '{}' all reconstruct to {} on read — treat as equal."""
    return v if v not in (None, "{}", "") else "{}"


def _tables(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def check_preconditions(conn: sqlite3.Connection) -> Tuple[bool, List[str]]:
    """Return (ok, violations). ok == True means folios is provably redundant and
    safe to drop (or already absent). Any violation is fail-closed."""
    conn.row_factory = sqlite3.Row
    tbls = _tables(conn)
    if "folios" not in tbls:
        return (True, [])  # already dropped / born folios-free — no-op
    if "refs" not in tbls or "versions" not in tbls:
        return (False, ["missing refs/versions (pre-Phase-2 schema); refuse to drop"])
    v: List[str] = []

    # folios ⊆ refs
    n = conn.execute(
        "SELECT COUNT(*) FROM folios f WHERE NOT EXISTS "
        "(SELECT 1 FROM refs r WHERE r.slug = f.folio_id)").fetchone()[0]
    if n:
        v.append(f"{n} folio(s) not in refs (would lose an unmigrated folio)")

    # nonopen_no_thread: non-open folios.status with no covering status thread
    n = conn.execute(
        "SELECT COUNT(*) FROM folios f WHERE COALESCE(f.status,'open') != 'open' "
        "AND NOT EXISTS (SELECT 1 FROM threads t WHERE t.type='status' AND "
        "  t.to_id = (SELECT r.genesis_hash FROM refs r WHERE r.slug = f.folio_id))"
    ).fetchone()[0]
    if n:
        v.append(f"{n} non-open folio(s) with no covering status thread "
                 f"(status would be lost)")

    # assigned_to / archived empty ecosystem-wide
    n = conn.execute(
        "SELECT COUNT(*) FROM folios WHERE assigned_to IS NOT NULL").fetchone()[0]
    if n:
        v.append(f"{n} folio(s) with a non-null folios.assigned_to")
    n = conn.execute(
        "SELECT COUNT(*) FROM folios WHERE COALESCE(archived,0) != 0").fetchone()[0]
    if n:
        v.append(f"{n} folio(s) with folios.archived set")

    # target_agent / omlet / acknowledged_at / metadata mirrored in refs;
    # identity/site recoverable from refs⋈versions.
    mism = {c: 0 for c in _MIRROR_COLS}
    mism["metadata"] = 0
    identity_mism = {
        "head_hash_not_in_versions": 0,
        "head_version_not_self_verifying": 0,
        "content_hash_head_hash": 0,
        "site_id_refs_site_id": 0,
        "identity_hash_head_hash": 0,
    }
    for f in conn.execute("SELECT * FROM folios"):
        r = conn.execute("SELECT * FROM refs WHERE slug=?",
                         (f["folio_id"],)).fetchone()
        if r is None:
            continue  # already counted by folios ⊆ refs above
        head = conn.execute("SELECT * FROM versions WHERE content_hash=?",
                            (r["head_hash"],)).fetchone()
        if head is None:
            identity_mism["head_hash_not_in_versions"] += 1
        elif KNURL_AVAILABLE:
            head_hash = compute_folio_hash({
                "type": head["type"],
                "title": head["title"],
                "content": head["content"],
                "created_at": head["created_at"],
                "created_by": head["created_by"],
            })
            if head_hash != r["head_hash"]:
                identity_mism["head_version_not_self_verifying"] += 1
        if (f["content_hash"] or "") != (r["head_hash"] or ""):
            identity_mism["content_hash_head_hash"] += 1
        if f["site_id"] != r["site_id"]:
            identity_mism["site_id_refs_site_id"] += 1
        if KNURL_AVAILABLE:
            f_hash = compute_folio_hash({
                "type": f["type"],
                "title": f["title"],
                "content": f["content"],
                "created_at": f["created_at"],
                "created_by": f["created_by"],
            })
            if f_hash != r["head_hash"]:
                identity_mism["identity_hash_head_hash"] += 1
        for col in _MIRROR_COLS:
            if f[col] != r[col]:
                mism[col] += 1
        if _norm_meta(f["metadata"]) != _norm_meta(r["metadata"]):
            mism["metadata"] += 1
    bad = {c: k for c, k in mism.items() if k}
    if bad:
        v.append(f"folios/refs mirror mismatch (unmirrored control): {bad}")
    if identity_mism["identity_hash_head_hash"]:
        v.append(f"{identity_mism['identity_hash_head_hash']} folio(s) whose identity "
                 f"does not hash to the head")
    if identity_mism["content_hash_head_hash"]:
        v.append(f"{identity_mism['content_hash_head_hash']} folio(s) with content_hash "
                 f"!= refs.head_hash")
    if identity_mism["site_id_refs_site_id"]:
        v.append(f"{identity_mism['site_id_refs_site_id']} folio(s) with site_id "
                 f"!= refs.site_id")
    if identity_mism["head_hash_not_in_versions"]:
        v.append(f"{identity_mism['head_hash_not_in_versions']} folio(s) whose "
                 f"head_hash is not in versions")
    if identity_mism["head_version_not_self_verifying"]:
        v.append(f"{identity_mism['head_version_not_self_verifying']} folio(s) whose "
                 f"head version does not self-verify")

    return (not v, v)


def _row_counts(conn: sqlite3.Connection) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("versions", "refs", "threads")}


def verify_dropped(conn: sqlite3.Connection, before: dict) -> Tuple[bool, List[str]]:
    """After the drop: folios is gone, versions/refs/threads are UNCHANGED, and a
    head read still resolves. Anything else is a blocker."""
    problems: List[str] = []
    if "folios" in _tables(conn):
        problems.append("folios table still present after drop")
    after = _row_counts(conn)
    for t, n in before.items():
        if after.get(t) != n:
            problems.append(f"{t} row count changed by the drop: {n} -> {after.get(t)}")
    # smoke: a head read over the join still works (drop touched nothing but folios)
    try:
        conn.execute(
            "SELECT COUNT(*) FROM refs r JOIN versions v ON v.content_hash = r.head_hash"
        ).fetchone()
    except sqlite3.Error as e:
        problems.append(f"head read broken after drop: {type(e).__name__}: {e}")
    return (not problems, problems)


def drop_one(pid: str, db: Path, *, live: bool, stamp: str) -> Tuple[bool, str, list]:
    """Precondition-check + drop + verify one db. Returns (passed, label, problems).
    dry-run: operate on a private temp COPY (the live db is untouched).
    live:    operate on the live db; the pre-image snapshot is the restore point."""
    tmp: Optional[Path] = None
    try:
        if live:
            prior = sorted(db.parent.glob(f"{db.name}.bak-folios-drop-*"))
            if prior:
                return (False, f"refused: prior folios-drop backup exists "
                        f"({prior[0].name}) — resolve by hand (the first .bak is the "
                        f"true pre-drop image)", ["prior folios-drop backup exists"])
            target = db
        else:
            tmp = Path(tempfile.mkdtemp(prefix="drop_folios_dry_"))
            target = tmp / "post.db"
            _backup_copy(db, target)

        # 1. precondition (fail-closed) — on the target we are about to drop
        chk = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            ok, violations = check_preconditions(chk)
            had_folios = "folios" in _tables(chk)
            before = _row_counts(chk) if had_folios else {}
            n_folios = (chk.execute("SELECT COUNT(*) FROM folios").fetchone()[0]
                        if had_folios else 0)
        finally:
            chk.close()
        if not ok:
            return (False, "precondition REFUSED", violations)
        if not had_folios:
            return (True, "no-op (folios already absent)", [])

        # 2. live restore point (== pre-drop image)
        if live:
            pre = db.with_name(f"{db.name}.bak-folios-drop-{stamp}")
            if pre.exists():
                return (False, f"refused: backup destination already exists "
                        f"({pre.name}) — resolve by hand (the first .bak is the true "
                        f"pre-drop image)", ["backup destination already exists"])
            tmp_pre = db.with_name(f".{pre.name}.tmp-{os.getpid()}")
            fd = os.open(tmp_pre, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                _backup_copy(db, tmp_pre)
                bc = sqlite3.connect(f"file:{tmp_pre}?mode=ro", uri=True)
                try:
                    if "folios" not in _tables(bc):
                        return (False, f"refused: backup missing folios ({pre.name})",
                                ["backup missing folios"])
                    backed_up = bc.execute("SELECT COUNT(*) FROM folios").fetchone()[0]
                    if backed_up != n_folios:
                        return (False, f"refused: backup folios count mismatch "
                                f"({backed_up} != {n_folios})",
                                ["backup folios count mismatch"])
                finally:
                    bc.close()
                try:
                    os.link(tmp_pre, pre)
                except FileExistsError:
                    return (False, f"refused: backup destination already exists "
                            f"({pre.name}) — resolve by hand (the first .bak is the "
                            f"true pre-drop image)",
                            ["backup destination already exists"])
            finally:
                tmp_pre.unlink(missing_ok=True)

        # 3. DROP (its indexes drop with it), in one immediate transaction
        w = sqlite3.connect(str(target), isolation_level=None, timeout=30)
        try:
            w.execute("PRAGMA busy_timeout = 30000")
            w.execute("BEGIN IMMEDIATE")
            try:
                w.execute("DROP TABLE folios")
                w.execute("COMMIT")
                if live:
                    try:
                        row = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        if row is not None and row[0]:
                            print(f"  WARN     {pid}: wal_checkpoint(TRUNCATE) "
                                  f"reported busy after drop: {tuple(row)}")
                    except sqlite3.Error as e:
                        print(f"  WARN     {pid}: wal_checkpoint(TRUNCATE) failed "
                              f"after drop: {type(e).__name__}: {e}")
            except Exception:
                w.execute("ROLLBACK")
                raise
        finally:
            w.close()

        # 4. verify
        vc = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            vok, problems = verify_dropped(vc, before)
        finally:
            vc.close()
        if not vok:
            return (False, "verify FAILED after drop", problems)
        return (True, f"dropped folios ({n_folios} rows) | versions={before['versions']} "
                f"refs={before['refs']} threads={before['threads']} unchanged", [])
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _order_targets(targets: List[Tuple[str, Path]]) -> List[Tuple[str, Path]]:
    """Smallest db first (build confidence), largest (speakbot) last."""
    return sorted(targets, key=lambda t: t[1].stat().st_size)


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 3a A5 Part 2 folios-drop driver.")
    p.add_argument("db_path", type=Path, nargs="?", help="a single skein.db")
    p.add_argument("--dry-run", action="store_true",
                   help="prove on COPIES; never write a live db (default)")
    p.add_argument("--live", action="store_true",
                   help="DROP on the LIVE dbs (requires --i-understand-this-drops-folios)")
    p.add_argument("--i-understand-this-drops-folios", action="store_true",
                   help="explicit confirmation gate for --live")
    args = p.parse_args()

    if args.live and args.dry_run:
        p.error("give one of --dry-run / --live")
    live = bool(args.live)
    if live and not args.i_understand_this_drops_folios:
        p.error("--live requires --i-understand-this-drops-folios (this DROPs real "
                "tables; quiesce skein.service first, restart on Part-1 code after)")
    if not live and not args.dry_run:
        args.dry_run = True

    targets = ([("(single)", args.db_path)] if args.db_path
               else _order_targets(_db_paths_from_registry()))
    if not targets:
        print("no databases found")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode = "LIVE — DROPPING REAL TABLES" if live else "dry-run (copies only)"
    print(f"[{mode}] A5 folios-drop over {len(targets)} db(s), smallest-first\n")
    ok = 0
    for pid, db in targets:
        try:
            passed, label, problems = drop_one(pid, db, live=live, stamp=stamp)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR    {pid}: {type(e).__name__}: {e}")
            print(f"\nABORTED at {pid}. {ok} db(s) done."
                  + (f" Restore from {db.name}.bak-folios-drop-{stamp} if it was "
                     "written." if live else ""))
            return 1
        if not passed:
            print(f"  BLOCKED  {pid}: {label}")
            for pr in problems[:12]:
                print(f"             - {pr}")
            print(f"\nABORTED at {pid} — a non-pass is a BLOCKER. {ok} db(s) done."
                  + (" Earlier live dbs are restorable from their "
                     ".bak-folios-drop-<stamp>." if live else ""))
            return 1
        ok += 1
        print(f"  OK       {pid}: {label}")
    print(f"\nAll {ok} db(s) {'DROPPED + verified (LIVE)' if live else 'proven on copies'}."
          + (" Restart skein.service on master (Part-1: never writes folios)."
             if live else " Safe to proceed to --live when quiesced."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
