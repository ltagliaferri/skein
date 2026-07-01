"""Phase 3a property tests + Fell-to-Fixture regression corpus.

This is RSP phase-2 test design (``docs/PHASE_3A_TEST_DESIGN.md`` §2/§3): the
executable spec for the control-taxonomy migration, written **before** the A1–A5
implementation. Two kinds of test live here:

1. **Answer-key / fixture self-tests (GREEN now, and forever).** They lock the
   independent Leg C reducer (``tests.fixtures.phase3a_expected.expected_control``)
   and every per-branch fixture by asserting **hand-written literal** expected
   values — not ``reader == rebuild`` (the circular check §1 forbids), and not
   ``expected_control == expected_control``. These are the de-circularizer's own
   regression net: if a future edit breaks the reducer the oracle leans on, these
   go red. No production/migration code needed.

2. **Production specs (RED now → green as A1–A5 land).** They call the production
   reader / writer / migration over the fixtures and assert the result equals the
   independently-computed expected value (literal or reducer). They are genuinely
   failing today because the code they call does not exist yet — that is the
   test-first load. Each carries ``@pytest.mark.phase3a_pending`` so a regression
   health check can exclude them (``pytest -m 'not phase3a_pending'`` stays green
   on the rest of the suite and on the kind-1 tests above). As each Ax step lands,
   its specs turn green; **remove the marker from a test once it passes for real.**

The named ``fell:<artifact>:r<round>:<slug>`` ids in the docstrings are the
Fell-to-Fixture pattern (§2): every confirmed finding from the Phase 3 design fell
(7 rounds, opus + codex) gets a permanent regression test, so a caught defect can
never silently return. The id ties the test back to the exact fell round/finding.

The A2–A5 production entry points these specs pin (the contract the implementation
must satisfy) are documented inline at each section. They are intentionally
minimal — the specs assert **observable database state** wherever possible, so
they do not couple to a return shape, only to an entry point.

Most of this corpus is pure logic over the synthetic fixtures (§6 step 2). Two
kinds of spec reach past that, deliberately: the A2 genesis-keyed WRITE behavior is
constructed route-side, so ``TestA2ControlWritesGenesisKeyed`` drives it end-to-end
through the API (TestClient + store override); and the A5 consumer-survival specs
drop the folios table on a live store. Both still run hermetically (temp store, no
network).

Scope boundary (what this corpus does NOT cover, by design):
  - Audit count-reconciliation to fidelity-baseline movement, and the
    "baselines move by exactly the logged drop count" cross-check: those run
    against real dbs and the fidelity harness — the differential **oracle**
    (``verify_threads_control.py``) and the live A3 run own them, not a hermetic
    fixture unit. Here the audit is pinned only for the orphan-category
    distinction (r4:genesis-orphan-log), the one thing observable solely via it.
  - Real-hash identity and breadth across the 45 live dbs: the oracle's ``--all``
    copy-proof sweep owns that. These fixtures are synthetic, so they pin
    migration **logic and shapes**, not real-hash identity (§4 of the doc).

Every behavioral Fell-to-Fixture id from §2 maps to at least one test here (grep
the ``fell:phase3-design:`` tags); the reducer-side of each lands GREEN now, the
production-side RED until its step.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pytest

from skein.identity import compute_thread_hash
from tests.fixtures import phase3a_synth as synth
from tests.fixtures.phase3a_expected import ARCHIVED_MARKER, expected_control


# ── shared plumbing ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp(prefix="phase3a_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _build(name: str, tmp_dir: Path) -> Path:
    """Build one named synthetic fixture db under tmp_dir; return its path."""
    return synth.build_fixture(name, tmp_dir)


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _expected(path: Path) -> Dict[str, Dict[str, object]]:
    conn = _ro(path)
    try:
        return expected_control(conn)
    finally:
        conn.close()


# The hand-written, independent answer key. Derived by reading each fixture in
# ``phase3a_synth`` and applying the design rule (§5.A1/§5.A3) BY HAND — not by
# running the reducer. Asserting ``expected_control(fixture) == this`` locks the
# reducer against these literals, so a reducer bug shared with the production
# path cannot hide. ``i1_genesis_collision`` raises (the I1 precondition) and is
# checked separately. Every other fixture has exactly one live folio (or none, for
# the orphan shapes that drop to ``{}``).
NAMED_EXPECTED: Dict[str, Dict[str, Dict[str, object]]] = {
    "status_plain_selfloop": {
        "status-plain-selfloop-folio": {"status": "confirmed", "assigned_to": None, "archived": 0}},
    "status_nonselfloop_agent": {
        "status-nonselfloop-folio": {"status": "confirmed", "assigned_to": None, "archived": 0}},
    "status_slug_orphan": {},
    "status_genesis_orphan": {},
    "status_already_genesis": {
        "status-already-genesis-folio": {"status": "confirmed", "assigned_to": None, "archived": 0}},
    "status_overlap_agent_deleted": {},
    "status_tie_group": {
        # (created_at, thread_id) max: 'thread-status-tie-bbb' > '...-aaa' at equal
        # created_at, so 'confirmed' (bbb) wins, NOT 'open' (aaa).
        "status-tie-folio": {"status": "confirmed", "assigned_to": None, "archived": 0}},
    "thread_byte_duplicate": {
        "byte-dup-folio": {"status": "confirmed", "assigned_to": None, "archived": 0}},
    "status_multi_history": {
        "status-multi-history-folio": {"status": "closed", "assigned_to": None, "archived": 0}},
    "status_mixed_keyspace": {
        # genesis-keyed row is LATER here -> 'closed' (both union-latest and the
        # prefer-genesis shortcut agree; the slug_later fixture is what splits them).
        "status-mixed-keyspace-folio": {"status": "closed", "assigned_to": None, "archived": 0}},
    "status_mixed_keyspace_slug_later": {
        # slug-keyed row is LATER -> union-latest returns 'closed'; prefer-genesis
        # would wrongly return the stale 'open'.
        "status-mixed-keyspace-slug-later-folio": {"status": "closed", "assigned_to": None, "archived": 0}},
    "assignment_plain": {
        "assignment-plain-folio": {"status": "open", "assigned_to": "agent-assignee", "archived": 0}},
    "assignment_orphan": {},
    "assignment_odd_shape": {
        # to_id is the folio's own slug (the eq68 odd-shape row); the assignee value
        # the reducer surfaces is that to_id verbatim — odd, but preserved, not fixed up.
        "assignment-odd-shape-folio": {"status": "open", "assigned_to": "assignment-odd-shape-folio", "archived": 0}},
    "archive_selfloop": {
        "archive-selfloop-folio": {"status": "open", "assigned_to": None, "archived": 1}},
}

# Every fixture except the one that raises must appear in the answer key, so a new
# fixture can't be added without an expected literal (coverage forcing-function).
_RAISES = {"i1_genesis_collision"}
assert set(synth.ALL_FIXTURES) - _RAISES == set(NAMED_EXPECTED), (
    "NAMED_EXPECTED must name every non-raising fixture (add the literal when you "
    "add a fixture): "
    f"missing={set(synth.ALL_FIXTURES) - _RAISES - set(NAMED_EXPECTED)} "
    f"extra={set(NAMED_EXPECTED) - (set(synth.ALL_FIXTURES) - _RAISES)}")


# ══════════════════════════════════════════════════════════════════════════════
# 1. Answer-key / fixture self-tests — GREEN now. Lock the Leg C de-circularizer.
# ══════════════════════════════════════════════════════════════════════════════

class TestAnswerKey:
    """The independent reducer (``expected_control``) reduces every fixture to its
    hand-written expected control map. This is the answer key the oracle's Leg C
    imports; locking it here is what makes a reduction bug shared by the A1 reader
    and the A3 rebuild *detectable* (the bug is absent from this reducer)."""

    @pytest.mark.parametrize("name", sorted(NAMED_EXPECTED))
    def test_reducer_matches_hand_written_expected(self, name, tmp_dir):
        path = _build(name, tmp_dir)
        assert _expected(path) == NAMED_EXPECTED[name]

    def test_i1_collision_raises_precondition(self, tmp_dir):
        """``i1_genesis_collision``: two lineages share a genesis. The reducer's
        ``_ref_maps`` raises rather than silently last-wins — the I1 precondition
        (design §3 A3) is enforced at the answer key too, so a bad snapshot is a
        loud failure, not a quiet wrong reduction."""
        path = _build("i1_genesis_collision", tmp_dir)
        conn = _ro(path)
        try:
            with pytest.raises(ValueError, match="I1 violation"):
                expected_control(conn)
        finally:
            conn.close()


class TestDecircularizerDistinctions:
    """The subtle reductions that a naive shortcut would get wrong. These are the
    ones that earn the de-circularizer its keep, so they are asserted explicitly
    (not just folded into the parametrized sweep above)."""

    def test_union_reader_not_prefer_genesis(self, tmp_dir):
        """fell:phase3-design:r1:union-reader — the reducer unions slug- and
        genesis-keyed rows and takes the LATEST, never 'prefer genesis, else slug'.
        The slug_later fixture has the slug-keyed row later, so union-latest = the
        slug value ('closed') while the shortcut would return the stale genesis
        value ('open'). Asserting 'closed' is what rejects the shortcut."""
        path = _build("status_mixed_keyspace_slug_later", tmp_dir)
        got = _expected(path)["status-mixed-keyspace-slug-later-folio"]
        assert got["status"] == "closed"
        assert got["status"] != "open"  # the prefer-genesis (stale) value

    def test_tiebreaker_names_the_winning_row(self, tmp_dir):
        """fell:phase3-design:r4:tiebreaker-determinism — at equal created_at the
        winner is the max thread_id ('thread-status-tie-bbb' -> 'confirmed'), the
        SPECIFIC row the (created_at, thread_id) rule must pick — not merely
        'reader and rebuild agree', and not the lexically-smaller-content row."""
        path = _build("status_tie_group", tmp_dir)
        got = _expected(path)["status-tie-folio"]
        assert got["status"] == "confirmed"   # thread-status-tie-bbb (max thread_id)
        assert got["status"] != "open"        # thread-status-tie-aaa (min thread_id)

    def test_two_typed_branches_do_not_cross_contaminate(self, tmp_dir):
        """fell:phase3-design:r2:two-typed-branches — status reduces on to_id,
        assignment on from_id; neither control kind drops or pollutes the other. A
        status-only folio has assigned_to=None; an assignment-only folio keeps
        status at its default 'open'."""
        status_only = _expected(_build("status_plain_selfloop", tmp_dir))[
            "status-plain-selfloop-folio"]
        assert status_only["status"] == "confirmed"
        assert status_only["assigned_to"] is None

        assign_only = _expected(_build("assignment_plain", tmp_dir))[
            "assignment-plain-folio"]
        assert assign_only["assigned_to"] == "agent-assignee"
        assert assign_only["status"] == "open"  # status untouched by the assignment

    def test_archive_path_is_separate_from_status(self, tmp_dir):
        """fell:phase3-design:r4:archive-path — an archive self-loop feeds
        ``archived`` (1 iff content == ARCHIVED_MARKER), NOT status; status stays
        at its 'open' default. The two readers map to different refs columns."""
        got = _expected(_build("archive_selfloop", tmp_dir))["archive-selfloop-folio"]
        assert got["archived"] == 1
        assert got["status"] == "open"

    def test_orphans_and_overlap_drop_to_nothing(self, tmp_dir):
        """fell:phase3-design:r2:overlap-precedence and r4:genesis-orphan-log
        (reducer side) — a thread whose anchor resolves to no live folio
        contributes nothing: the slug orphan, the genesis orphan, and the
        overlap (from=agent AND deleted slug) all reduce away rather than
        surfacing a spurious value or a NULL-keyed entry."""
        for name in ("status_slug_orphan", "status_genesis_orphan",
                     "status_overlap_agent_deleted", "assignment_orphan"):
            assert _expected(_build(name, tmp_dir)) == {}, name

    def test_present_none_status_coerces_to_open(self, tmp_dir):
        """The status-NULL residual (brief step 3): a folio whose LATEST status
        thread has content=NULL reduces to 'open' (the default), identical to a
        folio with no status thread — matching the oracle's _normalize_control so
        identical data can't diverge as a false-RED. Unreachable via the live
        writer (always a string), so it is constructed here."""
        path = _build("status_plain_selfloop", tmp_dir)
        conn = sqlite3.connect(str(path))
        # Null out the content of the (latest) status thread.
        conn.execute("UPDATE threads SET content = NULL WHERE type = 'status'")
        conn.commit()
        conn.close()
        got = _expected(path)["status-plain-selfloop-folio"]
        assert got["status"] == "open"


class TestThreadHashLogic:
    """Pure-logic facts about the content hash, independent of any wiring. These
    are GREEN (the hash function already exists and is correct); the A2 specs
    below test that the live writers actually STAMP it."""

    def test_distinct_created_at_distinct_hash(self):
        """fell:phase3-design:r1:dedup-created-at (logic side) — two control rows
        identical in every field EXCEPT created_at hash to DISTINCT values, because
        compute_thread_hash includes the normalized created_at. So re-asserting the
        same logical status at a new now() is NOT idempotent: both rows persist
        with different hashes. (The collapse is only of fully byte-identical rows.)"""
        base = dict(from_id="sha256::g", to_id="sha256::g", type="status",
                    weaver="agent-x", content="closed")
        h1 = compute_thread_hash(created_at="2026-01-01T00:00:00+00:00", **base)
        h2 = compute_thread_hash(created_at="2026-01-01T00:00:01+00:00", **base)
        assert h1 != h2

    def test_byte_identical_same_hash(self):
        """The flip side: rows identical across all six canonical fields (incl. the
        normalized created_at) hash equal — those are the only rows 3a would ever
        collapse (and it defers even that to the 3b PK swap)."""
        fields = dict(from_id="sha256::g", to_id="sha256::g", type="status",
                      weaver="agent-x", content="closed",
                      created_at="2026-01-01T00:00:00+00:00")
        assert compute_thread_hash(**fields) == compute_thread_hash(**fields)


# ══════════════════════════════════════════════════════════════════════════════
# Production-spec helpers — load the not-yet-built code, fail RED if absent.
# ══════════════════════════════════════════════════════════════════════════════

def _logdb(path: Path):
    """Construct a LogDatabase over a db (runs _init_db; idempotent). Imported
    lazily so a future storage refactor can't break collection of the GREEN
    tests above."""
    from skein.storage import LogDatabase
    return LogDatabase(str(path))


def _require_method(obj, name: str):
    """Return obj.<name> or fail RED with a test-first message (pre-A1 schema)."""
    fn = getattr(obj, name, None)
    if fn is None:
        pytest.fail(f"{type(obj).__name__}.{name}() not implemented yet — "
                    f"RED until its A1–A5 step lands")
    return fn


def _load_migration():
    """The A3/A4 control migration module. The contract these specs pin (minimal —
    an entry point + a typed refusal + two observable A4 helpers):

      migrate_db(db_path, *, dry_run=False) -> result
          Runs the re-anchor + thread_hash backfill + refs rebuild in one
          transaction. ``result.dropped`` is a list of dicts each carrying at least
          ``thread_id`` and ``category`` ('slug_orphan' | 'genesis_orphan').
      PreconditionError
          The exception migrate_db raises when it REFUSES a db (I1 genesis
          collision, or cache-only control with no covering thread) — a typed
          refusal, so a spec can assert refusal without accepting an incidental crash.
      slug_keyed_control_remaining(db_path) -> int
          Count of control threads still anchored on a live slug (0 once a db is
          fully re-anchored) — the observable the A4 gate reads.
      all_dbs_migrated(db_paths) -> bool
          The A4 gate: True iff EVERY db in ``db_paths`` has zero slug-keyed
          control (so the genesis-only reader flip is safe). False if any db still
          holds slug-keyed control."""
    try:
        from skein.migrations import migrate_threads_control as m
    except ImportError as e:
        pytest.fail(f"A3 migration (skein.migrations.migrate_threads_control) not "
                    f"implemented yet — RED until A3 lands: {e}")
    return m


# ══════════════════════════════════════════════════════════════════════════════
# 2. A1 — tolerant reader + new archive reader. RED now → green at A1.
# ══════════════════════════════════════════════════════════════════════════════

def _insert_thread(path: Path, thread_id, from_id, to_id, ttype, content,
                   weaver, created_at):
    """Insert one raw thread row (for constructing reader-specific shapes that are
    not A3-transform branches, so they live here, not in the canonical synth set)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO threads (thread_id, from_id, to_id, type, content, weaver, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (thread_id, from_id, to_id, ttype, content, weaver, created_at))
    conn.commit()
    conn.close()


class TestA1ReaderInvariants:
    """The slug-keyed read paths A1 must NOT regress. These are GREEN now and stay
    green after A1 — they lock that making the readers tolerant of genesis keys does
    not break the all-slug-keyed case that is the whole ecosystem today."""

    def test_slug_keyed_status_reads_under_slug(self, tmp_dir):
        path = _build("status_plain_selfloop", tmp_dir)
        db = _logdb(path)
        assert db.get_latest_statuses() == {"status-plain-selfloop-folio": "confirmed"}

    def test_slug_keyed_assignment_reads_under_slug(self, tmp_dir):
        path = _build("assignment_plain", tmp_dir)
        db = _logdb(path)
        assert db.get_latest_assignments() == {"assignment-plain-folio": "agent-assignee"}


class TestA1TolerantReader:
    """The A1 readers (get_latest_statuses / get_latest_assignments /
    get_latest_archives) return a per-SLUG control map that unions the slug- and
    genesis-keyspaces and resolves genesis hashes back to their slug. Expected values
    come from the independent reducer / literals, never reader == rebuild. GREEN as
    of the A1 landing (marker removed); each shape is one the pre-A1 reader got wrong
    (genesis-keyed surfacing, or a missing reader)."""

    def _statuses(self, path: Path) -> Dict[str, object]:
        db = _logdb(path)
        return _require_method(db, "get_latest_statuses")()

    def test_all_genesis_keyed_shape(self, tmp_dir):
        """A genesis-keyed status (the A2-window write) is resolved back to its
        slug, not surfaced under the bare genesis hash (today's reader keys it on
        the raw to_id = genesis hash, so this is red)."""
        path = _build("status_already_genesis", tmp_dir)
        assert self._statuses(path) == {"status-already-genesis-folio": "confirmed"}

    def test_split_keyspace_unions_to_latest(self, tmp_dir):
        """fell:phase3-design:r1:union-reader (reader side) — a folio with both a
        slug-keyed and a genesis-keyed status reduces to the single latest value
        under one slug key, not two endpoint keys."""
        path = _build("status_mixed_keyspace", tmp_dir)
        assert self._statuses(path) == {"status-mixed-keyspace-folio": "closed"}

    def test_split_keyspace_slug_later_picks_slug(self, tmp_dir):
        """fell:phase3-design:r1:union-reader (reader side) — the distinguishing
        ordering: with the slug-keyed row later, union-latest returns the slug
        value, not the stale genesis one (rejects prefer-genesis-else-slug in the
        production reader, the same way the answer-key test does for the reducer)."""
        path = _build("status_mixed_keyspace_slug_later", tmp_dir)
        assert self._statuses(path) == {"status-mixed-keyspace-slug-later-folio": "closed"}

    def test_tiebreaker_named_winner(self, tmp_dir):
        """fell:phase3-design:r4:tiebreaker-determinism (reader side) — two
        GENESIS-keyed status rows at equal created_at: the reader resolves them to
        the slug and deterministically picks the max-thread_id row ('...-bbb' ->
        'confirmed'), the SPECIFIC row the (created_at, thread_id) rule must pick.
        Genesis-keyed on purpose so today's raw-to_id reader is red regardless of
        how it breaks the tie."""
        path = _build("status_plain_selfloop", tmp_dir)
        slug = "status-plain-selfloop-folio"
        g = _genesis_of(path, slug)
        # Drop the fixture's own slug-keyed status so only the genesis tie remains.
        conn = sqlite3.connect(str(path))
        conn.execute("DELETE FROM threads WHERE type = 'status'")
        conn.commit()
        conn.close()
        tie_ts = "2026-02-01T00:00:00+00:00"
        _insert_thread(path, "tie-genesis-bbb", g, g, "status", "confirmed", "a", tie_ts)
        _insert_thread(path, "tie-genesis-aaa", g, g, "status", "open", "a", tie_ts)
        assert self._statuses(path) == {slug: "confirmed"}

    def test_genesis_keyed_assignment_resolves_to_slug(self, tmp_dir):
        """fell:phase3-design:r2:two-typed-branches (reader side) — the assignment
        reader keys the folio on from_id and unions keyspaces: a genesis-keyed
        assignment (from = genesis) resolves back to its slug, not the bare hash."""
        path = _build("assignment_plain", tmp_dir)
        slug = "assignment-plain-folio"
        g = _genesis_of(path, slug)
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE threads SET from_id = ? WHERE type = 'assignment'", (g,))
        conn.commit()
        conn.close()
        db = _logdb(path)
        assert _require_method(db, "get_latest_assignments")() == {slug: "agent-assignee"}

    def test_archive_reader_exists_and_reads_marker(self, tmp_dir):
        """fell:phase3-design:r4:archive-path (reader side) — get_latest_archives is
        a DEDICATED reader (analogous to get_latest_statuses, self-loop on to_id)
        feeding refs.archived; it is NOT folded into the status reader. It reads the
        archive marker, and the status reader must not surface the archive row."""
        path = _build("archive_selfloop", tmp_dir)
        db = _logdb(path)
        archives = _require_method(db, "get_latest_archives")()
        # The archive folio is archived; its status reader sees no status thread.
        assert archives.get("archive-selfloop-folio") == ARCHIVED_MARKER
        statuses = _require_method(db, "get_latest_statuses")()
        assert "archive-selfloop-folio" not in statuses

    def test_thread_hash_column_added_nullable_nonunique(self, tmp_dir):
        """A1 adds threads.thread_hash as a nullable, NON-unique column (no UNIQUE
        index in 3a — that is 3b). Today the column is absent, so this is red."""
        path = _build("status_plain_selfloop", tmp_dir)
        _logdb(path)  # A1's _init_db adds the column
        conn = _ro(path)
        try:
            cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(threads)")}
            assert "thread_hash" in cols, "thread_hash column missing (pre-A1)"
            assert cols["thread_hash"]["notnull"] == 0, "thread_hash must be nullable in 3a"
            idx = conn.execute("PRAGMA index_list(threads)").fetchall()
            unique_cols = set()
            for i in idx:
                if i["unique"]:
                    for c in conn.execute(f"PRAGMA index_info({i['name']})"):
                        unique_cols.add(c["name"])
            assert "thread_hash" not in unique_cols, "thread_hash must be NON-unique in 3a"
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. A2 — writers emit genesis-keyed control + stamp thread_hash. RED → green @ A2.
# ══════════════════════════════════════════════════════════════════════════════

def _thread_hash_of(path: Path, thread_id: str) -> Optional[str]:
    conn = _ro(path)
    try:
        row = conn.execute(
            "SELECT thread_hash FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return row["thread_hash"] if row is not None else None
    finally:
        conn.close()


@pytest.mark.phase3a_pending
class TestA2Writers:
    """A2 wires compute_thread_hash into every insert site (design §2.4: four in
    all). Three are live runtime paths, exercised here:
      - save_thread (storage.py:1096) — directly;
      - the _maintain_versions_refs SUPERSEDES INSERT (storage.py:1412) — via a
        forward save_folio edit (identity field changed);
      - the _maintain_versions_refs REVERTED INSERT (storage.py:1394) — a DISTINCT
        INSERT in a DIFFERENT branch (revert / DAG re-entry), via a save_folio
        content A->B->A. Missing this one lets a revert land a NULL-hash row and
        break 3b's NOT-NULL PK swap, so it gets its own spec.
    The fourth site, the backfill REPAIR-mode supersedes mint
    (backfill_versions_refs.py:~286), is a migration TOOL: the design's A2 escape
    clause (§5.A2) lets it be frozen (not run between A2 and the 3b PK swap) rather
    than wired. Its disposition (stamp vs freeze) is the A2 implementer's choice and
    is verified at the A2 fell, not by a hermetic runtime unit here."""

    def _make_thread(self, **kw):
        from skein.models import Thread
        base = dict(thread_id="t-a2-1", from_id="sha256::g", to_id="sha256::g",
                    type="status", content="closed", weaver="agent-x",
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        base.update(kw)
        return Thread(**base)

    def test_save_thread_stamps_thread_hash(self, tmp_dir):
        """fell:phase3-design:r1:all-insert-sites-hashed (save_thread site) — a
        thread saved through save_thread lands with a non-NULL thread_hash equal to
        compute_thread_hash over its canonical fields. No new row may land NULL
        (3b's NOT-NULL PK depends on it)."""
        path = tmp_dir / "skein.db"
        db = _logdb(path)
        th = self._make_thread()
        db.save_thread(th)
        got = _thread_hash_of(path, th.thread_id)
        assert got is not None
        assert got == compute_thread_hash(
            th.from_id, th.to_id, th.type, th.weaver, th.created_at, th.content)

    def test_reassert_distinct_now_distinct_hash_two_rows(self, tmp_dir):
        """fell:phase3-design:r1:dedup-created-at (wiring side) — re-asserting the
        same logical status at a later now() yields a SECOND row with a distinct
        thread_hash; re-assert is not idempotent and the row is not collapsed in 3a."""
        path = tmp_dir / "skein.db"
        db = _logdb(path)
        db.save_thread(self._make_thread(
            thread_id="t-a2-r1", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        db.save_thread(self._make_thread(
            thread_id="t-a2-r2", created_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)))
        h1, h2 = _thread_hash_of(path, "t-a2-r1"), _thread_hash_of(path, "t-a2-r2")
        assert h1 is not None and h2 is not None and h1 != h2
        conn = _ro(path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM threads WHERE type='status'").fetchone()[0]
        finally:
            conn.close()
        assert n == 2  # both rows persist

    def test_edit_supersedes_edge_is_hashed(self, tmp_dir):
        """fell:phase3-design:r1:all-insert-sites-hashed (_maintain_versions_refs
        site) — an edit that mints a supersedes edge (the direct INSERT inside
        _maintain_versions_refs) stamps thread_hash too, so no insert path leaks a
        NULL-hash row."""
        from skein.models import Folio
        path = tmp_dir / "skein.db"
        db = _logdb(path)
        f = Folio(folio_id="edit-folio-1", type="issue", site_id="s",
                  created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                  created_by="agent-x", title="Title one here",
                  content="original body", status="open", metadata={})
        db.save_folio(f)
        f.content = "edited body — identity changed, mints a supersedes edge"
        db.save_folio(f)
        conn = _ro(path)
        try:
            nulls = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE type='supersedes' "
                "AND thread_hash IS NULL").fetchone()[0]
            total = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE type='supersedes'").fetchone()[0]
        finally:
            conn.close()
        assert total >= 1 and nulls == 0

    def test_edit_reverted_marker_is_hashed(self, tmp_dir):
        """fell:phase3-design:r1:all-insert-sites-hashed (_maintain_versions_refs
        REVERTED site, storage.py:1394) — the reverted marker is a SEPARATE INSERT
        in a SEPARATE branch from supersedes: a revert (content A->B->A) mints it.
        A2 must stamp thread_hash there too, or a revert lands a NULL-hash row.
        Distinct from test_edit_supersedes_edge_is_hashed, which only hits the
        forward branch."""
        from skein.models import Folio
        path = tmp_dir / "skein.db"
        db = _logdb(path)
        f = Folio(folio_id="revert-folio-1", type="issue", site_id="s",
                  created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                  created_by="agent-x", title="Title one here",
                  content="content A — the original body", status="open", metadata={})
        db.save_folio(f)
        f.content = "content B — a forward edit that mints a supersedes edge"
        db.save_folio(f)
        f.content = "content A — the original body"  # revert -> mints a 'reverted' marker
        db.save_folio(f)
        conn = _ro(path)
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE type='reverted'").fetchone()[0]
            nulls = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE type='reverted' "
                "AND thread_hash IS NULL").fetchone()[0]
        finally:
            conn.close()
        assert total >= 1 and nulls == 0


@pytest.mark.phase3a_pending
class TestA2ControlWritesGenesisKeyed:
    """A NEW control write through the API lands GENESIS-keyed, not slug-keyed
    (design §3 A2 "a new control write lands genesis-keyed"; §5.A2). This is the
    core A2 behavior the A4 genesis-only reader depends on: if a fresh status or
    assignment write stayed slug-keyed after the flip, that folio would read 'open'.
    The genesis endpoints are constructed in the routes.py writers, so — unlike the
    hermetic units above — this is exercised end-to-end through the API (TestClient +
    store override, the established pattern in test_sites_timestamps.py). Red today
    (the writers emit from=to=folio_id, routes.py:844); green once A2 keys control
    writes on the folio's genesis hash."""

    def _client_store(self, tmp_dir):
        from fastapi.testclient import TestClient
        from skein_server import app
        from skein.routes import get_project_store
        from skein.storage import JSONStore
        store = JSONStore(tmp_dir)
        app.dependency_overrides[get_project_store] = lambda: store
        return TestClient(app), store, app, get_project_store

    def _genesis(self, store, slug):
        conn = sqlite3.connect(str(store._log_db.db_path))
        try:
            row = conn.execute(
                "SELECT genesis_hash FROM refs WHERE slug = ?", (slug,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _create_folio(self, client, **extra):
        client.post("/skein/sites",
                    json={"site_id": "s1", "purpose": "testing genesis-keyed writes"},
                    headers={"X-Agent-Id": "agent-x"})
        payload = {"type": "finding", "site_id": "s1",
                   "title": "A folio title here", "content": "body content"}
        payload.update(extra)
        r = client.post("/skein/folios", json=payload,
                        headers={"X-Agent-Id": "agent-x"})
        assert r.status_code == 200, r.text
        return r.json()["folio_id"]

    def test_status_write_lands_genesis_keyed(self, tmp_dir):
        """A status set via the API mints a self-loop keyed on the folio's GENESIS
        hash (from == to == genesis), not the slug."""
        client, store, app, dep = self._client_store(tmp_dir)
        try:
            fid = self._create_folio(client, metadata={"status": "closed"})
            genesis = self._genesis(store, fid)
            assert genesis is not None and genesis != fid
            st = store.get_threads(type="status")
            assert st, "no status thread minted"
            for t in st:
                assert t.from_id == genesis and t.to_id == genesis, (
                    f"status thread not genesis-keyed: from={t.from_id} to={t.to_id}")
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_assignment_write_lands_genesis_keyed_from(self, tmp_dir):
        """fell:phase3-design:r2:two-typed-branches (writer side) — an assignment set
        via the API keys FROM on the folio's genesis hash, leaving TO (the assignee)
        untouched."""
        client, store, app, dep = self._client_store(tmp_dir)
        try:
            fid = self._create_folio(client, assigned_to="agent-assignee-z")
            genesis = self._genesis(store, fid)
            assert genesis is not None and genesis != fid
            asg = store.get_threads(type="assignment")
            assert asg, "no assignment thread minted"
            for t in asg:
                assert t.from_id == genesis, (
                    f"assignment 'from' not genesis-keyed: from={t.from_id}")
                assert t.to_id == "agent-assignee-z"  # assignee left untouched
        finally:
            app.dependency_overrides.pop(dep, None)


# ══════════════════════════════════════════════════════════════════════════════
# 4. A3 — the migration. RED now → green at A3. Observable-state assertions.
# ══════════════════════════════════════════════════════════════════════════════

def _threads_of_type(path: Path, ttype: str):
    conn = _ro(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT thread_id, from_id, to_id, type, content, weaver, created_at "
            "FROM threads WHERE type = ? ORDER BY thread_id", (ttype,))]
    finally:
        conn.close()


def _all_threads_full(path: Path):
    """Every thread row, ALL columns including thread_hash, deterministically
    ordered — the full byte-image used to prove idempotency (a rerun that rewrote a
    hash, dropped a row, or mutated any field would move this)."""
    conn = _ro(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT thread_id, from_id, to_id, type, content, weaver, created_at, "
            "thread_hash FROM threads ORDER BY thread_id")]
    finally:
        conn.close()


def _genesis_of(path: Path, slug: str) -> Optional[str]:
    conn = _ro(path)
    try:
        row = conn.execute(
            "SELECT genesis_hash FROM refs WHERE slug = ?", (slug,)).fetchone()
        return row["genesis_hash"] if row else None
    finally:
        conn.close()


def _refs_control(path: Path) -> Dict[str, Dict[str, object]]:
    """The post-rebuild refs cache, normalized to the comparison form used by the
    reducer's value contract (None status -> 'open'; archived -> 0/1)."""
    conn = _ro(path)
    try:
        out = {}
        for r in conn.execute(
                "SELECT slug, status, assigned_to, archived FROM refs"):
            out[r["slug"]] = {
                "status": "open" if r["status"] is None else r["status"],
                "assigned_to": r["assigned_to"],
                "archived": 1 if r["archived"] else 0,
            }
        return out
    finally:
        conn.close()


@pytest.mark.phase3a_pending
class TestA3Migration:
    """The A3 re-anchor migration, asserted by OBSERVABLE database state so the
    specs pin behavior, not an implementation shape. The one interface assumption
    is the entry point ``migrate_threads_control.migrate_db(path)`` (and, for the
    orphan-category test, ``result.dropped``)."""

    def test_plain_selfloop_rekeyed_to_genesis(self, tmp_dir):
        """slug status self-loop -> re-keyed to from=to=genesis."""
        path = _build("status_plain_selfloop", tmp_dir)
        genesis = _genesis_of(path, "status-plain-selfloop-folio")
        _load_migration().migrate_db(path)
        rows = _threads_of_type(path, "status")
        assert len(rows) == 1
        assert rows[0]["from_id"] == genesis and rows[0]["to_id"] == genesis

    def test_nonselfloop_normalized_agent_to_weaver(self, tmp_dir):
        """from=agent, to=slug -> normalized to genesis self-loop, the setting
        agent preserved in weaver (24bk #2), not lost and not left on from_id."""
        path = _build("status_nonselfloop_agent", tmp_dir)
        genesis = _genesis_of(path, "status-nonselfloop-folio")
        _load_migration().migrate_db(path)
        rows = _threads_of_type(path, "status")
        assert len(rows) == 1
        assert rows[0]["from_id"] == genesis and rows[0]["to_id"] == genesis
        assert rows[0]["weaver"] == synth.AGENT_SETTER

    def test_a2_window_row_passes_through_untouched(self, tmp_dir):
        """fell:phase3-design:r3:a2-window-passthrough — a status already
        genesis-keyed (matching a live refs.genesis_hash) is passed through
        byte-for-byte, never re-resolved as a slug and dropped."""
        path = _build("status_already_genesis", tmp_dir)
        before = _threads_of_type(path, "status")
        _load_migration().migrate_db(path)
        after = _threads_of_type(path, "status")
        assert len(after) == 1
        for k in ("from_id", "to_id", "content", "weaver", "created_at"):
            assert after[0][k] == before[0][k], k

    def test_slug_orphan_dropped_with_category(self, tmp_dir):
        """fell:phase3-design:r4:genesis-orphan-log (slug side) — a status whose
        slug has no ref is dropped and logged with category 'slug_orphan'."""
        path = _build("status_slug_orphan", tmp_dir)
        result = _load_migration().migrate_db(path)
        assert _threads_of_type(path, "status") == []
        cats = {d["category"] for d in result.dropped}
        assert "slug_orphan" in cats

    def test_genesis_orphan_dropped_with_distinct_category(self, tmp_dir):
        """fell:phase3-design:r4:genesis-orphan-log — a genesis-shaped endpoint
        matching NO live refs.genesis_hash is dropped under a DISTINCT category
        'genesis_orphan' (not conflated with a slug orphan, not passed through)."""
        path = _build("status_genesis_orphan", tmp_dir)
        result = _load_migration().migrate_db(path)
        assert _threads_of_type(path, "status") == []
        cats = {d["category"] for d in result.dropped}
        assert "genesis_orphan" in cats

    def test_overlap_dropped_never_null_from(self, tmp_dir):
        """fell:phase3-design:r2:overlap-precedence — a row that is BOTH
        non-self-loop (from=agent) AND orphan (deleted slug) is DROPPED, never
        normalized to from_id=NULL (which would violate from_id NOT NULL)."""
        path = _build("status_overlap_agent_deleted", tmp_dir)
        _load_migration().migrate_db(path)
        assert _threads_of_type(path, "status") == []
        conn = _ro(path)
        try:
            nulls = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE from_id IS NULL").fetchone()[0]
        finally:
            conn.close()
        assert nulls == 0

    def test_assignment_orphan_and_oddshape(self, tmp_dir):
        """fell:phase3-design:r1:assignment-orphan-oddshape — the orphan assignment
        is dropped; the odd-shape assignment (to_id is the folio's own slug) has its
        FROM re-keyed to genesis with its TO left untouched, never blanket-rewritten
        to from_id=NULL."""
        path = _build("assignment_orphan", tmp_dir)
        _load_migration().migrate_db(path)
        assert _threads_of_type(path, "assignment") == []

        path2 = _build("assignment_odd_shape", tmp_dir)
        slug = "assignment-odd-shape-folio"
        genesis = _genesis_of(path2, slug)
        _load_migration().migrate_db(path2)
        rows = _threads_of_type(path2, "assignment")
        assert len(rows) == 1
        assert rows[0]["from_id"] == genesis    # from re-keyed
        assert rows[0]["to_id"] == slug         # odd to_id left as-is

    def test_thread_hash_backfilled_for_every_row(self, tmp_dir):
        """A3 backfills thread_hash for EVERY surviving row (all types), so no row
        is left NULL-hash for 3b's PK swap."""
        path = _build("status_multi_history", tmp_dir)
        _load_migration().migrate_db(path)
        conn = _ro(path)
        try:
            nulls = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE thread_hash IS NULL").fetchone()[0]
        finally:
            conn.close()
        assert nulls == 0

    @pytest.mark.parametrize("name", sorted(set(NAMED_EXPECTED)))
    def test_rebuilt_refs_equal_independent_expected(self, name, tmp_dir):
        """fell:phase3-design:r6:archive-equivalence — the post-A3 refs control
        cache equals the independently-computed expected map (the reducer over the
        PRE-A3 snapshot) for ALL THREE columns including ``archived`` (the
        archive_selfloop param proves refs.archived is rebuilt, not just
        status/assigned_to). This is the property the oracle's Leg C enforces over
        real dbs; here it is pinned over every fixture. Captures the expected BEFORE
        migrating (the reducer needs the original slug-keyed data)."""
        path = _build(name, tmp_dir)
        expected = _expected(path)        # over pre-A3 (slug-keyed) snapshot
        _load_migration().migrate_db(path)
        assert _refs_control(path) == expected

    @pytest.mark.parametrize("name", [
        "status_multi_history",     # status re-key + reduction history
        "status_nonselfloop_agent",  # status normalize (agent -> weaver)
        "status_mixed_keyspace",    # mixed keyspace (already-genesis + re-key)
        "assignment_plain",         # assignment re-key (from only)
        "archive_selfloop",         # archive control kind
    ])
    def test_idempotent_second_run_is_noop(self, name, tmp_dir):
        """Re-running A3 on already-migrated data changes NOTHING — asserted over
        the FULL thread image (all columns incl thread_hash) AND the refs control
        cache, across every control kind, not just status rows. A rerun that
        rewrote a thread_hash, mutated refs, dropped a row, or was non-idempotent
        for assignment/archive would move one of these snapshots."""
        path = _build(name, tmp_dir)
        m = _load_migration()
        m.migrate_db(path)
        threads1, refs1 = _all_threads_full(path), _refs_control(path)
        result2 = m.migrate_db(path)
        assert _all_threads_full(path) == threads1
        assert _refs_control(path) == refs1
        assert list(result2.dropped) == []

    def test_i1_collision_refused_as_precondition(self, tmp_dir):
        """I1 precondition: a db with two lineages sharing a genesis is REFUSED
        before any transform runs (the migration asserts I1, never assumes it)."""
        path = _build("i1_genesis_collision", tmp_dir)
        m = _load_migration()
        # A typed refusal, not any Exception — an incidental crash that never
        # reached the I1 check must NOT satisfy this safety-critical spec.
        with pytest.raises(m.PreconditionError):
            m.migrate_db(path)

    def test_cache_only_control_refused_as_precondition(self, tmp_dir):
        """Cache-only state (design §1, Fell finding 5): a folio whose refs cache
        carries non-default control with NO covering thread would be silently
        cleared by the rebuild. A3 REFUSES it as a precondition (before the
        rebuild), rather than losing the state. Constructed here (eq68 shows 0 such
        rows ecosystem-wide, but the gate enforces rather than assumes)."""
        path = _build("status_plain_selfloop", tmp_dir)
        conn = sqlite3.connect(str(path))
        # refs says 'closed' but there is no covering closed thread (the only
        # status thread is 'confirmed'); delete every status thread to make the
        # refs value purely cache-only.
        conn.execute("UPDATE refs SET status = 'closed' "
                     "WHERE slug = 'status-plain-selfloop-folio'")
        conn.execute("DELETE FROM threads WHERE type = 'status'")
        conn.commit()
        conn.close()
        m = _load_migration()
        with pytest.raises(m.PreconditionError):  # typed refusal, not any crash
            m.migrate_db(path)


# ══════════════════════════════════════════════════════════════════════════════
# 5. A4 / A5 — reader genesis-only gate; folios drop survival. RED → green.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.phase3a_pending
class TestA4Gate:
    def test_all_dbs_gate_refuses_flip_while_slug_keyed_control_remains(self, tmp_dir):
        """fell:phase3-design:r1:a4-alldbs-gate — A4 (reader becomes genesis-only)
        must not be reachable while ANY db still holds slug-keyed control threads,
        else every non-open folio there reads 'open'. This pins the PRODUCTION gate
        (all_dbs_migrated / slug_keyed_control_remaining), not a local count: a
        slug-keyed db has >0 slug-keyed control and the gate is False; a migrated db
        has 0 and the gate is True on its own; and a mixed set is still False (one
        un-migrated db blocks the whole flip)."""
        m = _load_migration()
        slug_db = _build("status_plain_selfloop", tmp_dir)
        migrated = _build("status_plain_selfloop", tmp_dir / "m")
        m.migrate_db(migrated)

        assert m.slug_keyed_control_remaining(slug_db) > 0
        assert m.slug_keyed_control_remaining(migrated) == 0
        assert m.all_dbs_migrated([migrated]) is True
        assert m.all_dbs_migrated([slug_db]) is False
        assert m.all_dbs_migrated([migrated, slug_db]) is False  # one blocks all


def _drop_folios(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE IF EXISTS folios")
    conn.commit()
    conn.close()


def _make_folio(folio_id, site_id="site-a", **kw):
    from skein.models import Folio
    base = dict(folio_id=folio_id, type="issue", site_id=site_id,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                created_by="agent-x", title="A folio title here",
                content="body", status="open", metadata={})
    base.update(kw)
    return Folio(**base)


class TestReadsSurviveFoliosDrop:
    """Reads have been off the folios table since Phase-2 commit C, so the
    list/enrich read path already survives a folios drop. GREEN now — locks that
    A5's drop does not regress a read that was supposed to be folios-free already.
    The same LogDatabase object is reused after the drop so _init_db cannot
    resurrect the table."""

    def test_get_folios_survives_folios_drop(self, tmp_dir):
        path = tmp_dir / "skein.db"
        db = _logdb(path)
        db.save_folio(_make_folio("listed-1"))
        _drop_folios(path)
        got = db.get_folios(site_id="site-a")  # SAME db object, no re-init
        assert any(f.folio_id == "listed-1" for f in got)


@pytest.mark.phase3a_pending
class TestA5FoliosConsumersSurviveDrop:
    """A5 drops folios.status and the folios table and retires every folios-touching
    consumer (design §3 A5). The ones that still reference folios today raise
    'no such table: folios' after the drop — RED until A5 removes the folios access.
    Covered here: the two writer paths (save_folio dual-write, move_folio's separate
    write) and migrate_folios_from_json (which counts folios). The SAME LogDatabase
    object is reused after the drop (a fresh one would re-run _init_db and resurrect
    folios, masking the regression).

    The dead ``FROM folios`` fallbacks (storage.py:200/254/305) are NOT
    behaviorally testable: they sit behind ``not _has_refs_table(conn)``, and every
    live db is backfilled (refs present) since Phase 1/2, so the guard is always
    True and the fallback branch never executes — it cannot be driven to raise. Its
    removal is a source cleanup verified at the A5 fell (the branch merely *names* a
    dropped table); the reachable folios consumers are the ones pinned here."""

    def test_move_folio_survives_folios_drop(self, tmp_dir):
        """fell:phase3-design:r1:move-folio-after-drop — POST /folios/{id}/move (->
        move_folio) returns the moved folio, not a 'no such table: folios' 500,
        after the folios drop. move_folio must stop its separate folios write
        (storage.py:1517) and keep only the refs.site_id write."""
        path = tmp_dir / "skein.db"
        db = _logdb(path)
        db.save_folio(_make_folio("move-me-1", site_id="site-a"))
        _drop_folios(path)
        result = db.move_folio("move-me-1", "site-b")  # SAME db object
        assert result is not None
        conn = _ro(path)
        try:
            site = conn.execute(
                "SELECT site_id FROM refs WHERE slug = 'move-me-1'").fetchone()["site_id"]
        finally:
            conn.close()
        assert site == "site-b"

    def test_save_folio_edit_survives_folios_drop(self, tmp_dir):
        """The save_folio dual-write to folios (storage.py:1320-1345) must be
        retired in A5: after the drop, editing a folio updates versions/refs and
        does not raise on the missing folios table."""
        path = tmp_dir / "skein.db"
        db = _logdb(path)
        f = _make_folio("edit-me-1")
        db.save_folio(f)
        _drop_folios(path)
        f.content = "edited body after the folios table was dropped"
        db.save_folio(f)  # SAME db object; must not touch folios
        assert _genesis_of(path, "edit-me-1") is not None

    def test_migrate_folios_from_json_survives_folios_drop(self, tmp_dir):
        """fell:phase3-design:r1:move-folio-after-drop (sibling consumer) —
        migrate_folios_from_json counts `SELECT COUNT(*) FROM folios`
        (storage.py:1692); A5 must gate or remove it so it does not crash on the
        dropped table. An existing but empty sites_dir passes its early exists()
        guard and reaches the folios query, so this is red on the missing table
        today and green once A5 gates the path."""
        path = tmp_dir / "skein.db"
        db = _logdb(path)
        sites_dir = tmp_dir / "sites"
        sites_dir.mkdir()  # exists() -> proceeds past the early `return 0`
        _drop_folios(path)
        assert db.migrate_folios_from_json(sites_dir) == 0  # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# 6. Cross-cutting — copy-proof digest. GREEN now (the oracle helper exists).
# ══════════════════════════════════════════════════════════════════════════════

class TestCopyProofDigest:
    """The preimage digest (design §3 copy-proof) ties a proven copy to the live
    db. It is order-independent, stable, and MOVES when the control-thread set or
    refs control changes — so a proof can't pass on one preimage while a different
    one runs live. The digest function already exists in the oracle, so these are
    GREEN."""

    def test_digest_stable_and_order_independent(self, tmp_dir):
        from skein.migrations.verify_threads_control import preimage_digest
        path = _build("status_multi_history", tmp_dir)
        conn1 = _ro(path)
        conn2 = _ro(path)
        try:
            assert preimage_digest(conn1) == preimage_digest(conn2)
            assert preimage_digest(conn1).startswith("sha256:")
        finally:
            conn1.close()
            conn2.close()

    def test_digest_moves_when_control_changes(self, tmp_dir):
        from skein.migrations.verify_threads_control import preimage_digest
        path = _build("status_multi_history", tmp_dir)
        conn = _ro(path)
        try:
            before = preimage_digest(conn)
        finally:
            conn.close()
        w = sqlite3.connect(str(path))
        w.execute("INSERT INTO threads (thread_id, from_id, to_id, type, content, "
                  "weaver, created_at) VALUES "
                  "('extra-status', 'status-multi-history-folio', "
                  "'status-multi-history-folio', 'status', 'reopened', 'agent-x', "
                  "'2026-01-01T00:00:09+00:00')")
        w.commit()
        w.close()
        conn = _ro(path)
        try:
            after = preimage_digest(conn)
        finally:
            conn.close()
        assert before != after
