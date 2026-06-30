#!/usr/bin/env python3
"""Phase 3a differential oracle — the control-equivalence verifier (the spine).

Read-only sibling of ``verify_versions_refs.py`` (the Phase 1/2 B->C gate). Same
posture: it does **not** lean on "the harness is green" — it diffs the actual
data, per db, ``--all`` / ``--sample``, and any divergence is a BLOCKER that
exits non-zero. It also never mutates an input db: every real-reader call runs
over a private temp copy (consistent backup, incl. WAL), so the frozen pre-A3
snapshot and the live dbs are untouched.

The independence rule (the load-bearing one). The parent design mandates that the
A1 reader and the A3 rebuild share one reduction (same union, same
``(created_at, thread_id)`` order). So ``rebuilt_refs == A1_read`` and
``read_pre == read_post`` both validate the new reduction against a *copy of
itself* — a shared reduction bug passes those legs green while every affected
folio's status is silently wrong. To break the circularity the oracle proves
three independent legs plus structural checks, per db.

  Leg A — transform/shadow (design §1 "Leg A", §3 A3).
    Per-folio reduced control (status/assigned_to/archived) identical before vs
    after migration, both via the production A1 reader. Covers the re-keying.
    A read-only oracle can't see a value that no longer exists, so the pre side
    is a frozen pre-image: either a (pre_snapshot_db) — the A1 reader run over
    the pre-A3 snapshot — or a captured manifest file (``--manifest``).

  Leg B — rebuild mirror (design §1 "Leg B", §5.1).
    ``rebuilt refs.{status,assigned_to,archived}`` == the A1 tolerant read
    (``get_latest_statuses`` / ``get_latest_assignments`` /
    ``get_latest_archives``) over the post db, for all three control columns.
    Covers the cache rebuild *given* the reduction. Needs only the post db.

  Leg C — independent reduction, the de-circularizer (design §1 "Leg C", §6 step 2).
    ``expected_control`` from ``tests.fixtures.phase3a_expected`` — a hand-written
    reducer sharing **no code** with the A1 reader or the A3 rebuild — is run over
    the FROZEN PRE-A3 snapshot, and the post db's rebuilt refs are compared
    against that frozen expected map. Pre-A3 timing is mandatory: the reducer must
    see the *original* (un-re-anchored) data so it is independent of the A3
    transform — running it over post-A3 data would validate the rebuild against
    data the rebuild itself produced (a false green). A shared reduction bug in
    the reader+rebuild is absent from this reducer, so Leg C catches it.

  Structural checks on the post db (design §1, §3 A3):
    I1  genesis_hash unique per lineage (no two refs share a genesis).
    I2  control resolves folio -> refs.genesis_hash -> type-filtered threads:
        every control thread is genesis-keyed (status/archive self-loop
        from==to==genesis; assignment from==genesis), forward-resolvable against
        refs — never a bare-sha reverse lookup, never a surviving slug key.
    No threads.from_id is NULL; every threads row has a non-null thread_hash.

  Copy-proof (design §3, "Cross-cutting"):
    ``preimage_digest`` is a stable digest over the control-thread set + refs
    control of a *pre-migration* db. It ties a proven copy to the live db: the
    proof can't pass on one preimage while a different one runs live. ``--print-
    digest`` captures/confirms it; ``--expect-digest`` asserts it (BLOCKER on
    mismatch).

The gate (what counts as a pass). Leg C is the de-circularizing leg, so a FULL
"verified" requires it to have run over a pre-A3 snapshot that is digest-bound via
``--expect-digest`` (the §3 copy-proof — keying alone can't prove pre-A3 timing).
A migrated db checked with structural + Leg B only (no pre-snapshot, ``--all``, or
a manifest that runs Leg A but not Leg C), or with an unbound pre-snapshot, is
reported "incomplete" (not "verified") and exits non-zero — the tool never lets a
migrated db pass the gate without Leg C. ``--allow-partial`` accepts "incomplete"
dbs without a non-zero exit, for the deliberate structural+Leg B ``--all`` sweep.
A pre-A1 "not migrated" db stays clean (exit 0).

When the oracle runs (design §1 "When the oracle runs"). It depends on the A1
reader and the ``thread_hash`` column. On a pre-A1 db/codebase (either absent) it
reports a clean per-db status "not migrated (pre-A1 schema)" and a non-divergence
exit — never an unhandled exception. From A1 onward it runs the real legs: red
(legs diverge — data not yet re-anchored) through A2, green after A3.

Usage:
    python -m skein.migrations.verify_threads_control POST_DB
    python -m skein.migrations.verify_threads_control POST_DB --pre-snapshot PRE_DB
    python -m skein.migrations.verify_threads_control POST_DB --manifest pre.json
    python -m skein.migrations.verify_threads_control --all
    python -m skein.migrations.verify_threads_control --all --sample 200
    python -m skein.migrations.verify_threads_control --print-digest PRE_DB
    python -m skein.migrations.verify_threads_control --print-digest LIVE_DB --expect-digest sha256:...
    python -m skein.migrations.verify_threads_control --write-manifest pre.json PRE_DB
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make the repo root importable so a direct
# ``python skein/migrations/verify_threads_control.py`` (not just ``-m``) can
# reach skein.* and the tests.fixtures Leg C reducer.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skein.storage import LogDatabase, load_project_registry  # noqa: E402
from tests.fixtures.phase3a_expected import (  # noqa: E402  (the Leg C answer key)
    ARCHIVED_MARKER,
    expected_control,
)

CONTROL_COLS = ("status", "assigned_to", "archived")
# Control types and the endpoint column that anchors each to its folio (design
# §5.A1): status/archive are self-loops keyed on to_id; assignment is from=folio.
CONTROL_ANCHORS = (("status", "to_id"), ("archive", "to_id"), ("assignment", "from_id"))


# ── read-only / no-mutation plumbing ────────────────────────────────────────

@contextmanager
def _ro(db_path):
    """A read-only connection (row factory set). Never writes."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _materialized(db_path):
    """Yield a path to a private, writable temp copy of ``db_path``.

    The copy is a consistent snapshot (sqlite backup API, so it includes WAL
    content) taken over a read-only source connection. We run the production A1
    readers over the copy because ``LogDatabase.__init__`` calls ``_init_db()``
    (a write) — copying first keeps the oracle read-only on every input, the
    frozen pre-A3 snapshot included.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="vtc_"))
    copy = tmpdir / "copy.db"
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(copy))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        yield copy
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── schema detection (pre-A1 guard) ─────────────────────────────────────────

def _code_has_archive_reader() -> bool:
    """True once the A1 ``get_latest_archives`` reader exists in the codebase."""
    return hasattr(LogDatabase, "get_latest_archives")


def _db_has_thread_hash(conn) -> bool:
    """True once the A1 ``threads.thread_hash`` column exists (also False if the
    db has no threads table at all — a non-skein db reads as not-migrated)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    return "thread_hash" in cols


def _is_migrated(db_path) -> bool:
    """The oracle's real legs need BOTH the A1 reader (code) and the thread_hash
    column (db). Either missing => pre-A1 => report 'not migrated', never crash."""
    if not _code_has_archive_reader():
        return False
    with _ro(db_path) as conn:
        return _db_has_thread_hash(conn)


# ── value coercion (compare post-read values, not raw bytes) ─────────────────

def _as_archived(value) -> int:
    """Coerce an archive value to refs.archived's 0/1 under the strict marker model.

    The A1 reader is analogous to ``get_latest_statuses`` (slug -> content), so the
    pinned contract (``tests.fixtures.phase3a_expected.ARCHIVED_MARKER`` /
    finding-20260630-0r3x) is: a string is archived **iff** it equals
    ``ARCHIVED_MARKER`` — not "any non-blocklisted string" (which mis-read
    ``'active'`` and every other content as archived, a false-red on unarchive).
    ``None`` (no archive thread) -> 0; a pre-reduced 0/1/bool -> truthiness,
    defensively, so a reader that reduces to a flag still compares correctly.
    """
    if value is None:
        return 0
    if isinstance(value, str):
        return 1 if value == ARCHIVED_MARKER else 0
    return 1 if value else 0


def _normalize_control(status, assigned_to, archived) -> Dict[str, object]:
    """The one normalization shared by every comparison form, and identical to the
    Leg C answer key (``tests.fixtures.phase3a_expected.expected_control``).

    The contract (Fell finding: default-coercion asymmetry): a default applies
    **only on absence**, which every source encodes as ``None`` — a reader dict's
    missing key surfaces via ``.get()`` as ``None``; a refs row's empty column is
    SQL NULL -> ``None``; a manifest's missing key -> ``None``. A *present* falsy
    value (notably an empty-string ``status`` content) is **preserved**, never
    coerced to the default — the answer key applies its default only on
    ``.get(slug, default)`` key-absence, so coercing a present ``''`` here would
    diverge the two sides on identical data.
      status      -> ``'open'`` iff ``None``; a present ``''`` stays ``''``.
      assigned_to -> the value as-is (``None`` stays ``None``).
      archived    -> ``_as_archived(value)`` (``None`` -> 0; marker model).
    """
    return {
        "status": "open" if status is None else status,
        "assigned_to": assigned_to,
        "archived": _as_archived(archived),
    }


def _rebuilt_refs(conn) -> Dict[str, Dict[str, object]]:
    """The post-migration rebuilt control cache, one entry per refs.slug,
    normalized (via the shared helper) to the comparison form."""
    out: Dict[str, Dict[str, object]] = {}
    for r in conn.execute(
        "SELECT slug, status, assigned_to, archived FROM refs"
    ):
        out[r["slug"]] = _normalize_control(
            r["status"], r["assigned_to"], r["archived"])
    return out


def _a1_reads(copy_path) -> Dict[str, Dict[str, object]]:
    """The live thread-derived read (design §5.1 baseline): run the production A1
    tolerant readers over a (copy of a) db and reduce to a per-folio control map
    with defaults. Keyed over refs.slug UNION the readers' own keys, so a reader
    that surfaces a slug with no ref (an orphan it should have dropped) is not
    hidden — it shows up as a Leg B/A divergence rather than being silently
    skipped."""
    db = LogDatabase(str(copy_path))
    statuses = db.get_latest_statuses()
    assignments = db.get_latest_assignments()
    archives = db.get_latest_archives()
    with _ro(copy_path) as conn:
        slugs = {r["slug"] for r in conn.execute("SELECT slug FROM refs")}
    keys = slugs | set(statuses) | set(assignments) | set(archives)
    out: Dict[str, Dict[str, object]] = {}
    for k in keys:
        # ``.get(k)`` -> None means "no thread of that type", the absence the
        # shared helper defaults; a present value (incl. '') is preserved.
        out[k] = _normalize_control(
            statuses.get(k), assignments.get(k), archives.get(k))
    return out


def _diff_maps(leg: str, a: Dict[str, dict], b: Dict[str, dict],
               a_name: str, b_name: str) -> List[str]:
    """Diff two per-folio control maps. In the copy-proof flow the folio set is
    invariant across A3 (it re-keys/drops orphan threads, never deletes a live
    folio), so a slug present on one side only is a divergence, not noise."""
    problems: List[str] = []
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    if only_a:
        problems.append(f"{leg}: {len(only_a)} folio(s) in {a_name} but not "
                        f"{b_name}: {only_a[:5]}")
    if only_b:
        problems.append(f"{leg}: {len(only_b)} folio(s) in {b_name} but not "
                        f"{a_name}: {only_b[:5]}")
    for slug in sorted(set(a) & set(b)):
        for col in CONTROL_COLS:
            av, bv = a[slug][col], b[slug][col]
            if av != bv:
                problems.append(f"{leg} {slug}.{col}: {a_name}={av!r} != "
                                f"{b_name}={bv!r}")
    return problems


# ── the legs ────────────────────────────────────────────────────────────────

def leg_a(pre_copy, post_copy) -> List[str]:
    """Leg A — pre A1-read == post A1-read (pre side = a pre-A3 snapshot db)."""
    return _diff_maps("Leg A", _a1_reads(pre_copy), _a1_reads(post_copy),
                      "pre-read", "post-read")


def leg_a_manifest(pre_map, post_copy) -> List[str]:
    """Leg A — captured-manifest mode: frozen pre-image map == post A1-read."""
    return _diff_maps("Leg A", pre_map, _a1_reads(post_copy),
                      "manifest", "post-read")


def leg_b(post_copy, post_conn) -> List[str]:
    """Leg B — rebuilt refs == A1 tolerant read, all three control columns."""
    return _diff_maps("Leg B", _rebuilt_refs(post_conn), _a1_reads(post_copy),
                      "refs", "A1-read")


def leg_c(pre_copy, post_conn) -> Tuple[List[str], List[str]]:
    """Leg C — independent reduction over the frozen pre-A3 snapshot vs rebuilt
    refs, plus the two pre-image gates only the snapshot can prove. Returns
    (problems, warnings).

      - Cache-only control (design §1 A3 precondition, Fell finding 5): a folio
        with non-default refs control but no covering thread is silently cleared
        by the rebuild while all three legs stay green -> BLOCKER.
      - Pre-A3 timing sanity (Fell finding 4): a snapshot with zero control
        threads cannot confirm pre-A3 timing from keying (warn); and if the post
        db then carries non-default control, the rebuild produced control from
        nothing -> BLOCKER. Otherwise, a snapshot whose control threads are all
        genesis-keyed may be POST-A3 (the reducer would validate the rebuild
        against data it produced) -> warn to confirm timing.
    Note: keying alone cannot *prove* pre-A3 timing (a born-genesis pre-A3 db and
    a post-A3 db are both all-genesis). The copy-proof digest (--expect-digest)
    is the real proof; these checks are sanity + the zero-thread blocker.
    """
    problems: List[str] = []
    warnings: List[str] = []
    rebuilt = _rebuilt_refs(post_conn)
    with _ro(pre_copy) as pre_conn:
        problems += _cache_only_control_blockers(pre_conn)
        total, has_slug_keyed = _control_thread_keying(pre_conn)
        if total == 0:
            warnings.append(
                "pre-snapshot has 0 control threads — pre-A3 timing cannot be "
                "confirmed from thread keying (rely on --expect-digest)")
            nondefault = sorted(s for s, c in rebuilt.items() if _is_nondefault(c))
            if nondefault:
                problems.append(
                    f"Leg C: pre-snapshot has 0 control threads but post db has "
                    f"{len(nondefault)} folio(s) with non-default control "
                    f"{nondefault[:5]} — rebuild produced control from nothing")
        elif not has_slug_keyed:
            warnings.append(
                "pre-snapshot has control threads but none are slug-keyed — it "
                "may be a POST-A3 snapshot; Leg C could be a false green. Confirm "
                "this is the frozen pre-A3 snapshot (or bind it via --expect-digest).")
        expected = expected_control(pre_conn)
    problems += _diff_maps("Leg C", expected, rebuilt, "expected(pre-A3)",
                           "rebuilt refs")
    return problems, warnings


def _control_thread_keying(conn) -> Tuple[int, bool]:
    """Over a snapshot: (total control threads, any-anchor-is-a-live-slug). A
    pre-A3 snapshot is slug-keyed; a post-A3 one is all-genesis. Counts fully (no
    early break) so the ``total == 0`` blind spot in Leg C is detectable."""
    slugs = {r[0] for r in conn.execute("SELECT slug FROM refs")}
    total = 0
    has_slug_keyed = False
    for ttype, anchor in CONTROL_ANCHORS:
        for (a,) in conn.execute(
            f"SELECT {anchor} FROM threads WHERE type = ?", (ttype,)
        ):
            total += 1
            if a in slugs:
                has_slug_keyed = True
    return total, has_slug_keyed


def _is_nondefault(ctl: Dict[str, object]) -> bool:
    """True if a normalized control triple carries real (non-default) state."""
    return (ctl["status"] != "open"
            or ctl["assigned_to"] is not None
            or bool(ctl["archived"]))


def _cache_only_control_blockers(conn) -> List[str]:
    """A3 precondition (design §1, Fell finding 5), over the PRE-A3 snapshot: a
    folio whose refs cache carries non-default control (status != 'open', or
    assigned_to set, or archived truthy) with NO covering control thread of that
    type — anchored by slug OR genesis — would be silently cleared by the A3
    rebuild while Leg A/B/C all stay green. BLOCKER. (Empirically 0 today per
    finding-20260630-eq68, but the gate enforces it rather than assuming it.)"""
    slugs = set()
    genesis_to_slug: Dict[str, str] = {}
    refs: Dict[str, Dict[str, object]] = {}
    for r in conn.execute(
        "SELECT slug, genesis_hash, status, assigned_to, archived FROM refs"
    ):
        slugs.add(r["slug"])
        if r["genesis_hash"] is not None:
            genesis_to_slug[r["genesis_hash"]] = r["slug"]
        refs[r["slug"]] = _normalize_control(
            r["status"], r["assigned_to"], r["archived"])
    covered: Dict[str, set] = {ttype: set() for ttype, _ in CONTROL_ANCHORS}
    for ttype, anchor in CONTROL_ANCHORS:
        for (a,) in conn.execute(
            f"SELECT {anchor} FROM threads WHERE type = ?", (ttype,)
        ):
            if a in slugs:
                covered[ttype].add(a)
            elif a in genesis_to_slug:
                covered[ttype].add(genesis_to_slug[a])
    problems: List[str] = []
    for slug, ctl in sorted(refs.items()):
        if ctl["status"] != "open" and slug not in covered["status"]:
            problems.append(
                f"cache-only control (A3 precondition) {slug}.status="
                f"{ctl['status']!r}: non-default but no covering status thread "
                f"(rebuild would clear it)")
        if ctl["assigned_to"] is not None and slug not in covered["assignment"]:
            problems.append(
                f"cache-only control (A3 precondition) {slug}.assigned_to="
                f"{ctl['assigned_to']!r}: set but no covering assignment thread "
                f"(rebuild would clear it)")
        if ctl["archived"] and slug not in covered["archive"]:
            problems.append(
                f"cache-only control (A3 precondition) {slug}.archived: truthy "
                f"but no covering archive thread (rebuild would clear it)")
    return problems


# ── structural invariants (post db) ─────────────────────────────────────────

def structural(conn, *, sample: int = 0) -> List[str]:
    """I1, I2, from_id-not-null, thread_hash-not-null. ``sample`` caps only the
    O(threads) I2 scan (the large one); it never caps a leg comparison."""
    problems: List[str] = []

    # I1: genesis_hash unique per lineage (no two refs share a genesis).
    genesis_to_slug: Dict[str, str] = {}
    for r in conn.execute("SELECT slug, genesis_hash FROM refs"):
        g, s = r["genesis_hash"], r["slug"]
        if g in genesis_to_slug and genesis_to_slug[g] != s:
            problems.append(f"I1: genesis_hash {g!r} shared by lineages "
                            f"{genesis_to_slug[g]!r} and {s!r}")
        else:
            genesis_to_slug[g] = s
    genesis_set = set(genesis_to_slug)

    # No NULL from_id; every row content-addressed (non-null thread_hash).
    n = conn.execute("SELECT COUNT(*) FROM threads WHERE from_id IS NULL").fetchone()[0]
    if n:
        problems.append(f"{n} thread(s) with NULL from_id")
    n = conn.execute("SELECT COUNT(*) FROM threads WHERE thread_hash IS NULL").fetchone()[0]
    if n:
        problems.append(f"{n} thread(s) with NULL thread_hash")

    # I2: every control thread is genesis-keyed and forward-resolvable against
    # refs (folio -> refs.genesis_hash -> threads), never a surviving slug key.
    # status/archive are self-loops (from==to==genesis); assignment is
    # from==genesis (to is the assignee agent, left untouched).
    for ttype, anchor in CONTROL_ANCHORS:
        rows = conn.execute(
            "SELECT thread_id, from_id, to_id FROM threads WHERE type = ?",
            (ttype,),
        ).fetchall()
        if sample:
            rows = rows[:sample]
        for row in rows:
            a = row[anchor]
            if a not in genesis_set:
                problems.append(
                    f"I2 {ttype} {row['thread_id']}: anchor ({anchor})={a!r} is "
                    f"not a refs.genesis_hash (not genesis-keyed / not "
                    f"forward-resolvable)")
                continue
            if ttype in ("status", "archive") and row["from_id"] != row["to_id"]:
                problems.append(
                    f"I2 {ttype} {row['thread_id']}: not a self-loop "
                    f"(from={row['from_id']!r} to={row['to_id']!r})")
    return problems


# ── copy-proof digest ───────────────────────────────────────────────────────

def preimage_digest(conn) -> str:
    """Stable digest over the control-thread set + refs control of a
    *pre-migration* db (design §3 copy-proof). Order-independent (rows sorted),
    canonical JSON, sha256. Does not read thread_hash, so it works on a pre-A1
    db. Tie a proven copy to the live db by asserting both have this digest."""
    payload = {"control_threads": [], "refs": []}
    for ttype in ("status", "assignment", "archive"):
        for r in conn.execute(
            "SELECT thread_id, from_id, to_id, type, content, weaver, created_at "
            "FROM threads WHERE type = ? ORDER BY thread_id",
            (ttype,),
        ):
            payload["control_threads"].append([
                r["thread_id"], r["from_id"], r["to_id"], r["type"],
                r["content"], r["weaver"], str(r["created_at"]),
            ])
    for r in conn.execute(
        "SELECT slug, genesis_hash, status, assigned_to, archived FROM refs "
        "ORDER BY slug"
    ):
        payload["refs"].append([
            r["slug"], r["genesis_hash"], r["status"], r["assigned_to"],
            (1 if r["archived"] else 0),
        ])
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _digest_eq(got: str, expected: str) -> bool:
    """Compare digests, tolerant of an optional ``sha256:`` prefix and case."""
    def norm(d: str) -> str:
        d = d.strip().lower()
        return d.split(":", 1)[1] if d.startswith("sha256:") else d
    return norm(got) == norm(expected)


# ── manifest (the captured pre-image, Leg A) ────────────────────────────────

def compute_manifest(db_path) -> Dict[str, Dict[str, object]]:
    """The pre-image manifest = the A1-reader reduced control map, per folio
    (design §1 Leg A). Read-only. A3 captures this just before it mutates; Leg A
    diffs the post read against it. Requires A1 schema."""
    with _materialized(db_path) as copy:
        return _a1_reads(copy)


def _load_manifest(path) -> Dict[str, Dict[str, object]]:
    raw = json.loads(Path(path).read_text())
    out: Dict[str, Dict[str, object]] = {}
    for slug, ctl in raw.items():
        # Same shared normalization as the live reads — a missing key -> None ->
        # default, a present '' preserved (Fell finding: default-coercion asymmetry).
        out[slug] = _normalize_control(
            ctl.get("status"), ctl.get("assigned_to"), ctl.get("archived"))
    return out


# ── per-db verification ─────────────────────────────────────────────────────

def verify_db(post_db, *, pre_snapshot=None, manifest=None, sample: int = 0,
              expect_digest: Optional[str] = None) -> Tuple[str, List[str], List[str]]:
    """Verify one db. Returns (label, problems, warnings).

    label is one of:
      "not migrated (pre-A1 schema)" — pre-A1; legs skipped, clean (exit 0).
      "verified"   — FULL gate pass: structural + Leg A + Leg B + Leg C, with the
                     pre-snapshot digest-bound (--expect-digest), so pre-A3 timing
                     is proven by the copy-proof.
      "incomplete" — migrated, no divergence found, but the full gate did not run:
                     Leg C was skipped (no/manifest-only pre-image) or ran over an
                     unbound snapshot (no --expect-digest, so timing unproven). A
                     migrated db must not pass the gate on structural + Leg B
                     alone — that path never runs the de-circularizing reducer.
      "diverged"   — a BLOCKER fired.
    ``problems`` are BLOCKERS (exit non-zero). Read-only on every input.
    """
    if not _is_migrated(post_db):
        why = ("get_latest_archives reader absent in code"
               if not _code_has_archive_reader()
               else "threads.thread_hash column absent")
        return ("not migrated (pre-A1 schema)", [],
                [f"{why} — pre-A1; legs skipped (run from A1 onward)"])

    problems: List[str] = []
    warnings: List[str] = []
    leg_c_ran = False

    # Copy-proof: assert the preimage (the pre-snapshot) matches the proven copy.
    # The digest always binds the *preimage* (pre-migration), never the post db.
    if expect_digest is not None:
        if pre_snapshot is None:
            raise ValueError("expect_digest requires pre_snapshot (the preimage "
                             "to bind); the post db is not a preimage")
        with _ro(pre_snapshot) as conn:
            got = preimage_digest(conn)
        if not _digest_eq(got, expect_digest):
            problems.append(
                f"copy-proof: preimage digest {got} != expected {expect_digest} "
                f"(the proof does not apply to this preimage)")

    with _materialized(post_db) as post_copy:
        with _ro(post_copy) as post_conn:
            problems += structural(post_conn, sample=sample)
            problems += leg_b(post_copy, post_conn)
            if pre_snapshot is not None:
                with _materialized(pre_snapshot) as pre_copy:
                    problems += leg_a(pre_copy, post_copy)
                    cp, cw = leg_c(pre_copy, post_conn)
                    problems += cp
                    warnings += cw
                    leg_c_ran = True
            elif manifest is not None:
                problems += leg_a_manifest(_load_manifest(manifest), post_copy)
                warnings.append("manifest mode runs Leg A only — Leg C (the "
                                "independent reducer) skipped; not a full gate pass")
            else:
                warnings.append("no pre-snapshot/manifest — Leg A and Leg C "
                                "skipped (structural + Leg B only)")

    if problems:
        return ("diverged", problems, warnings)
    # A FULL pass needs Leg C run over a digest-bound pre-A3 snapshot (Finding 3:
    # never let a migrated db pass on structural + Leg B alone; Finding 4: the
    # copy-proof digest is what actually proves pre-A3 timing).
    if leg_c_ran and expect_digest is not None:
        return ("verified", problems, warnings)
    if leg_c_ran:
        warnings.append("Leg C ran but the pre-snapshot is unbound (no "
                        "--expect-digest) — pre-A3 timing not proven by the "
                        "copy-proof; treated as incomplete for the gate")
    return ("incomplete", problems, warnings)


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


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Phase 3a differential oracle: control equivalence "
                    "(Leg A/B/C + structural), read-only, divergence = BLOCKER.")
    p.add_argument("db_path", type=Path, nargs="?",
                   help="the post-migration db to verify (or, with "
                        "--print-digest/--write-manifest, the source db)")
    p.add_argument("--all", action="store_true",
                   help="verify every registered db (structural + Leg B per db). "
                        "single-db pre-snapshot legs are not available here, so "
                        "every migrated db reports 'incomplete' (use "
                        "--allow-partial to accept the structural+Leg B sweep)")
    p.add_argument("--allow-partial", action="store_true",
                   help="accept 'incomplete' dbs (Leg C never ran) without a "
                        "non-zero exit — for the deliberate structural+Leg B "
                        "sweep only; NOT a full Leg A/B/C gate pass")
    p.add_argument("--sample", type=int, default=0, metavar="N",
                   help="cap the per-db I2 thread scan to N rows (0 = all; the "
                        "leg comparisons are always full to avoid a false green)")
    p.add_argument("--pre-snapshot", type=Path, metavar="DB",
                   help="frozen pre-A3 snapshot db — enables Leg A (pre via A1 "
                        "reader) and Leg C (independent reducer). single-db only.")
    p.add_argument("--manifest", type=Path, metavar="JSON",
                   help="captured pre-image manifest — enables Leg A when no "
                        "pre-snapshot db. single-db only.")
    p.add_argument("--write-manifest", type=Path, metavar="JSON",
                   help="compute the pre-image manifest from db_path and write "
                        "it (read-only on the db); then exit.")
    p.add_argument("--print-digest", action="store_true",
                   help="print the copy-proof preimage digest of db_path (or "
                        "--pre-snapshot) and exit.")
    p.add_argument("--expect-digest", metavar="HEX",
                   help="assert the preimage digest equals HEX (BLOCKER on "
                        "mismatch). In oracle mode requires --pre-snapshot.")
    args = p.parse_args()

    # ── digest-only mode ─────────────────────────────────────────────────────
    if args.print_digest:
        target = args.pre_snapshot or args.db_path
        if target is None or args.all:
            p.error("--print-digest needs a db (db_path or --pre-snapshot) and "
                    "is incompatible with --all")
        with _ro(target) as conn:
            digest = preimage_digest(conn)
        print(digest)
        if args.expect_digest is not None and not _digest_eq(digest, args.expect_digest):
            print(f"MISMATCH: expected {args.expect_digest}")
            sys.exit(1)
        return

    # ── write-manifest mode ──────────────────────────────────────────────────
    if args.write_manifest is not None:
        if args.db_path is None or args.all:
            p.error("--write-manifest needs db_path and is incompatible with --all")
        if not _is_migrated(args.db_path):
            print("ERROR: cannot compute manifest on a pre-A1 schema "
                  "(get_latest_archives / thread_hash absent); capture the "
                  "manifest at pre-A3 (post-A1) time")
            sys.exit(1)
        manifest = compute_manifest(args.db_path)
        args.write_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        print(f"wrote pre-image manifest ({len(manifest)} folios) -> "
              f"{args.write_manifest}")
        return

    # ── oracle mode ──────────────────────────────────────────────────────────
    if bool(args.all) == bool(args.db_path):
        p.error("give exactly one of: a db_path, or --all")
    if args.all and (args.pre_snapshot or args.manifest):
        p.error("--pre-snapshot / --manifest are single-db only (not with --all)")
    if args.pre_snapshot and args.manifest:
        p.error("give at most one of --pre-snapshot / --manifest")
    if args.expect_digest is not None and not args.pre_snapshot:
        p.error("--expect-digest requires --pre-snapshot (the preimage to bind)")

    targets = (_db_paths_from_registry() if args.all
               else [(str(args.db_path), args.db_path)])
    bad = 0
    incomplete = 0
    for label_id, db in targets:
        try:
            status, problems, warnings = verify_db(
                db, pre_snapshot=args.pre_snapshot, manifest=args.manifest,
                sample=args.sample, expect_digest=args.expect_digest)
        except Exception as e:  # never let a bad db crash the sweep
            print(f"  ERROR {label_id}: {type(e).__name__}: {e}")
            bad += 1
            continue
        if status == "diverged":
            bad += 1
            print(f"  DIVERGED {label_id}: {len(problems)} problem(s)")
            for msg in problems[:20]:
                print(f"      - {msg}")
            if len(problems) > 20:
                print(f"      ... and {len(problems) - 20} more")
        elif status == "incomplete":
            incomplete += 1
            print(f"  INCOMPLETE {label_id}: migrated, no divergence found, but "
                  f"the full Leg A/B/C gate did not run")
        elif status.startswith("not migrated"):
            print(f"  SKIP {label_id}: {status}")
        else:
            print(f"  OK {label_id}: control equivalence holds (full gate)")
        for msg in warnings:
            print(f"      ! WARN {label_id}: {msg}")

    if bad:
        print(f"\n{bad} db(s) DIVERGED — Phase 3a control equivalence BLOCKED")
        sys.exit(1)
    if incomplete and not args.allow_partial:
        print(f"\n{incomplete} migrated db(s) INCOMPLETE — Leg C never ran, so the "
              f"full gate is not satisfied. Re-run with --pre-snapshot + "
              f"--expect-digest for a full pass, or pass --allow-partial to accept "
              f"a structural+Leg B sweep.")
        sys.exit(1)
    if incomplete:
        print(f"\nClean within scope, but {incomplete} migrated db(s) ran "
              f"structural + Leg B only (--allow-partial) — NOT a full Leg A/B/C "
              f"gate pass.")
        return
    print("\nAll clean — Phase 3a control equivalence holds (full gate, or not "
          "yet migrated).")


if __name__ == "__main__":
    main()
