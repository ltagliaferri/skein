"""Phase 3b test-first specs — thread_hash PK swap + the Class B structural DAG.

RSP phase-2 test design (``docs/PHASE_3B_DESIGN.md``): the executable spec for the
3b migration, written **before** the implementation. Each production spec carried
``@pytest.mark.phase3b_pending`` while its code did not exist, so the regression
health check (``pytest -m 'not phase3a_pending and not phase3b_pending'``) stayed
green through the test-first window; the marker was removed from each class as its
increment landed. As of impl 2–4 (migrate_db / get_threads_display / verify_db) the
whole file is green with no pending markers.

Two kinds of test live here:

1. **Independent-oracle self-tests (GREEN now, forever).** ``_expected_display`` is
   a hand-written shadow of the presentation view that shares **no code** with
   ``get_threads_display`` — the de-circularizer (mirroring 3a's Leg C). The read-view
   spec asserts ``get_threads_display() == _expected_display(...)``, never
   ``reader == reader``, so a reader/rebuild bug shared across the swap cannot pass
   green. ``TestOracleIsIndependent`` locks the shadow against hand-written literals.

2. **Production specs (RED now → green as 3b lands).** They call the not-yet-written
   migration / classifier / predicate / verifier and the extended reader over a real
   hermetic store and assert observable database state. They fail today because the
   code does not exist — the test-first load.

The named ``fell:phase3b-design:r<round>:<slug>`` ids tie a spec back to the exact
finding it locks from the 3b design fell (5 rounds, opus + codex), so a caught
defect can never silently return (the Fell-to-Fixture pattern).

The contract the implementation must satisfy is pinned in the loader docstrings
(``_load_pk_migration`` / ``_load_pk_verifier``). Specs assert observable state, so
they couple to an entry point, not a return shape, wherever possible.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from skein.identity import compute_thread_hash
from skein.models import Folio, Site, Thread
from skein.storage import JSONStore
from skein.utils import generate_thread_id

# NOTE: phase3b_pending was applied per production CLASS (never module-wide) so
# TestOracleIsIndependent (the de-circularizer regression net) always ran; every
# class is now landed/green and unmarked. The marker stays registered so the
# health-check command above remains valid.

# The six schema-global thread index names the swap must recreate after the
# DROP+RENAME (design §4.4 step 5 / §8 index-presence post-condition).
IDX_THREADS = {
    "idx_threads_from", "idx_threads_to", "idx_threads_type", "idx_threads_created",
    "idx_threads_to_type_created", "idx_threads_from_type_created",
}


# ── shared plumbing (real hermetic store, mirrors test_versions_refs_conformance) ─

@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp(prefix="phase3b_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_dir):
    return JSONStore(tmp_dir)


def _db(store) -> Path:
    return store.base_dir / "skein.db"


def _conn(store) -> sqlite3.Connection:
    c = sqlite3.connect(_db(store))
    c.row_factory = sqlite3.Row
    return c


def _make_site(store, site_id="alpha"):
    store.save_site(Site(site_id=site_id, created_at=datetime.now(timezone.utc),
                         created_by="tester", purpose="phase3b"))


def _folio(folio_id, content="body", *, site_id="alpha", type="finding",
           created_at=None, created_by="author", title="T"):
    return Folio(folio_id=folio_id, type=type, site_id=site_id,
                 created_at=created_at or datetime.now(timezone.utc),
                 created_by=created_by, title=title, content=content,
                 status="open", metadata={})


def _mk_lineage(store, folio_id, content="body", **kw) -> str:
    """Create a folio; return its genesis (== head) hash."""
    store.save_folio(_folio(folio_id, content, **kw))
    return _ref(store, folio_id)["genesis_hash"]


def _edit(store, folio_id, new_content, editor="editor") -> str:
    """Edit a folio's content (mints a supersedes edge); return the new head hash."""
    f = store.get_folio(folio_id)
    f.content = new_content
    store.save_folio(f, editor=editor)
    return _ref(store, folio_id)["head_hash"]


def _ref(store, slug) -> sqlite3.Row:
    c = _conn(store)
    try:
        return c.execute("SELECT * FROM refs WHERE slug=?", (slug,)).fetchone()
    finally:
        c.close()


def _post_thread(store, from_id, to_id, ttype, *, weaver="w", content=None,
                 created_at=None) -> None:
    """Post a non-control edge via the real writer (slug-keyed for Class B/C)."""
    store.save_thread(Thread(
        thread_id=generate_thread_id(), from_id=from_id, to_id=to_id, type=ttype,
        content=content, weaver=weaver,
        created_at=created_at or datetime.now(timezone.utc)))


def _rows(store, ttype=None) -> List[dict]:
    c = _conn(store)
    try:
        q = "SELECT * FROM threads"
        args = ()
        if ttype:
            q += " WHERE type=?"
            args = (ttype,)
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def _indexes(store) -> set:
    c = _conn(store)
    try:
        return {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        c.close()


def _pk_columns(store, table="threads") -> List[str]:
    c = _conn(store)
    try:
        return [r["name"] for r in c.execute(f"PRAGMA table_info({table})") if r["pk"]]
    finally:
        c.close()


# ── the contract the implementation must satisfy (pinned here) ────────────────

def _load_pk_migration():
    """The Phase 3b PK-swap migration module. The contract these specs pin:

      migrate_db(db_path, *, dry_run=False) -> result
          Rewrites `threads` with `thread_hash` as PK in ONE transaction: re-anchor
          Class B rows (slug -> folio hash, recompute thread_hash), collapse true
          byte-duplicates via INSERT OR IGNORE, recreate the six idx_threads_* after
          DROP+RENAME (§4.4). Class C rows (incl. orphans, §5.3) are KEPT unchanged
          (endpoints not re-anchored) and logged. `result.collapsed` is the list of
          dropped thread_hashes (byte-dup ONLY); `result.reanchored` the count of
          Class B rows re-keyed; `result.orphans` the kept-and-logged orphan rows.
          ``dry_run=True`` mutates nothing and returns the same plan (counts).
          Idempotent by short-circuit: a db already at the PK schema (thread_hash is
          the PK) is a no-op PASS, checked BEFORE the no-prior-backup precondition —
          so a re-run after a completed migration neither refuses nor rewrites.
      Preconditions (fail-closed, only on a not-yet-migrated db): `skein.service`
          verified inactive (the module VERIFIES quiescence; the operator performs
          it); I1 (genesis unique per lineage); no prior `<db>.bak-threads-pk-*`.
      PreconditionError
          Typed refusal: raised on an I1 genesis collision (two slugs share a
          genesis) OR a re-anchor collision (two DISTINCT edges would collapse) —
          never a silent drop (§4.4, §5.3). A spec asserts refusal without accepting
          an incidental crash.
      classify_row(row, *, slugs, versions) -> 'A' | 'B' | 'C'
          Per-row class. `row` is a dict/Row with from_id/to_id/type. `slugs` is the
          set of live refs.slug, `versions` the set of versions.content_hash. Control
          types -> 'A'; tag/message -> 'C' (override); from_id==to_id -> 'C'
          (self-loop); an endpoint resolving to neither slug nor version -> 'C'
          (orphan); otherwise both-folio distinct -> 'B'.
      manifest_eligible(row, *, versions) -> bool
          The Merkle-manifest admission predicate (§7), an explicit ALLOW-LIST: True
          iff type in {reference, mention, reply, succession, within, published,
          supersedes, reverted}, from_id != to_id, and BOTH endpoints in `versions`.
          A denylist ("not control/tag/message") would silently federate any NEW
          type; the allow-list must reject an unknown type. Class A self-loops also
          have version-hash endpoints, so both the type gate and from!=to are
          load-bearing (each must reject a case the other would admit).
    """
    try:
        from skein.migrations import threads_pk_swap as m
    except ImportError as e:  # pragma: no cover - the point is it's RED
        pytest.fail(f"3b PK-swap migration (skein.migrations.threads_pk_swap) not "
                    f"implemented yet — RED until 3b lands: {e}")
    return m


def _load_pk_verifier():
    """The Phase 3b post-swap STRUCTURAL verifier (sibling of verify_versions_refs.py).

    Read-only, self-contained on a single post-swap db — no before-image needed.
    (The DIFFERENTIAL equivalence legs — control read unchanged, Class B display
    unchanged, row-count reconciliation — are before/after SPEC comparisons here,
    not verify_db legs, because they need the pre-image the migration harness holds.)

      verify_db(db_path) -> (problems, warnings)
          `problems` are BLOCKERS. Post-swap structural checks (§8):
          - thread_hash is the declared PK and is UNIQUE (no surviving duplicate).
          - every thread_hash self-verifies: compute_thread_hash(row) == the stored PK.
          - no dangling endpoint: every from_id/to_id resolves (Class B/version rows
            resolve in versions; Class C/orphan endpoints resolve as slug/agent).
          - all six idx_threads_* present (an unindexed table passes value checks).
          - I1 holds on the post db (no genesis_hash shared across two refs.slug).
    """
    try:
        from skein.migrations import verify_threads_pk as v
    except ImportError as e:  # pragma: no cover
        pytest.fail(f"3b verifier (skein.migrations.verify_threads_pk) not "
                    f"implemented yet — RED until 3b lands: {e}")
    return v


# ── the independent shadow of the display view (the de-circularizer, GREEN now) ─

def _expected_display(rows: List[dict], refs_by_slug: Dict[str, dict],
                      *, from_id=None, to_id=None, type=None) -> List[tuple]:
    """Hand-written shadow of what get_threads_display MUST return post-swap, keyed
    only off the raw rows + refs. Shares NO code with get_threads_display. Returns a
    normalized set of (from, to, type, content) tuples in PRESENTATION space
    (genesis-anchored Class A control AND Class B rows shown slug-keyed;
    version-anchored edges byte-faithful). Used to assert the reader against an
    independent expectation, never against itself.
    """
    GENESIS_ANCHORED = {"status", "assignment", "archive",  # control (3a)
                        "reference", "mention", "reply", "succession", "within"}
    slug_of_genesis = {r["genesis_hash"]: s for s, r in refs_by_slug.items()}
    genesis_of_slug = {s: r["genesis_hash"] for s, r in refs_by_slug.items()}

    out = []
    for row in rows:
        f, t, ty = row["from_id"], row["to_id"], row["type"]
        # Present genesis-anchored rows at the slug; leave version-anchored
        # (supersedes/reverted/published) byte-faithful.
        if ty in GENESIS_ANCHORED:
            f = slug_of_genesis.get(f, f)
            t = slug_of_genesis.get(t, t)
        # Re-apply the requested slug filter in presentation space.
        if from_id is not None and f != from_id:
            continue
        if to_id is not None and t != to_id:
            continue
        if type is not None and ty != type:
            continue
        out.append((f, t, ty, row.get("content")))
    return sorted(out, key=lambda x: (x[2], x[0], x[1], x[3] or ""))


def _display_via_reader(store, **kw) -> List[tuple]:
    threads = store.get_threads_display(**kw)
    return sorted(((th.from_id, th.to_id, th.type, th.content) for th in threads),
                  key=lambda x: (x[2], x[0], x[1], x[3] or ""))


def _refs_by_slug(store) -> Dict[str, dict]:
    c = _conn(store)
    try:
        return {r["slug"]: dict(r) for r in c.execute("SELECT * FROM refs")}
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Oracle self-test — GREEN now. Lock the de-circularizer against literals.
# ══════════════════════════════════════════════════════════════════════════════

class TestOracleIsIndependent:
    """The shadow reducer reduces raw rows to the exact expected presentation view,
    asserted against hand-written literals (not against the production reader)."""

    def test_shadow_presents_genesis_anchored_at_slug(self):
        refs = {"finding-a": {"genesis_hash": "sha256::ga"},
                "finding-b": {"genesis_hash": "sha256::gb"}}
        rows = [
            # a genesis-anchored reference edge, stored genesis-keyed post-swap
            {"from_id": "sha256::ga", "to_id": "sha256::gb", "type": "reference",
             "content": None},
            # a version-anchored supersedes edge — must stay byte-faithful (hashes)
            {"from_id": "sha256::v2", "to_id": "sha256::v1", "type": "supersedes",
             "content": None},
        ]
        got = _expected_display(rows, refs)
        assert got == sorted([
            ("finding-a", "finding-b", "reference", None),
            ("sha256::v2", "sha256::v1", "supersedes", None),
        ], key=lambda x: (x[2], x[0], x[1], x[3] or ""))

    def test_shadow_slug_filter_matches_genesis_keyed_row(self):
        refs = {"finding-a": {"genesis_hash": "sha256::ga"},
                "finding-b": {"genesis_hash": "sha256::gb"}}
        rows = [{"from_id": "sha256::ga", "to_id": "sha256::gb",
                 "type": "reference", "content": None}]
        # A query by slug finds the genesis-keyed edge (the UNION half, §6.1).
        assert _expected_display(rows, refs, from_id="finding-a") == [
            ("finding-a", "finding-b", "reference", None)]
        # A raw genesis-hash query returns nothing (matches control, §6.1).
        assert _expected_display(rows, refs, from_id="sha256::ga") == []


# ══════════════════════════════════════════════════════════════════════════════
# 2. The PK swap — schema. RED until migrate_db lands.
# ══════════════════════════════════════════════════════════════════════════════

class TestPKSwapSchema:  # GREEN (impl 2): migrate_db landed
    def test_thread_hash_is_primary_key(self, store):
        _make_site(store)
        _mk_lineage(store, "finding-20260703-aaaa")
        _load_pk_migration().migrate_db(_db(store))
        assert _pk_columns(store) == ["thread_hash"], "thread_hash is the sole PK"

    def test_thread_id_kept_not_null(self, store):
        # fell:phase3b-design:r1:keep-thread-id — thread_id survives as the reader
        # tiebreaker + audit handle; dropping it would retarget the reduction.
        _make_site(store)
        _mk_lineage(store, "finding-20260703-bbbb")
        _load_pk_migration().migrate_db(_db(store))
        cols = {r["name"]: r for r in _conn(store).execute("PRAGMA table_info(threads)")}
        assert "thread_id" in cols and cols["thread_id"]["notnull"] == 1

    def test_all_six_indexes_present_after_swap(self, store):
        # fell:phase3b-design:r3:index-ordering — indexes recreated after DROP+RENAME.
        _make_site(store)
        _mk_lineage(store, "finding-20260703-cccc")
        _load_pk_migration().migrate_db(_db(store))
        assert IDX_THREADS <= _indexes(store), "all six idx_threads_* recreated"

    def test_duplicate_thread_hash_rejected_after_swap(self, store):
        _make_site(store)
        _mk_lineage(store, "finding-20260703-dddd", "v1")
        _edit(store, "finding-20260703-dddd", "v2")  # mint a supersedes row to dup
        _load_pk_migration().migrate_db(_db(store))
        c = _conn(store)
        try:
            row = c.execute("SELECT * FROM threads LIMIT 1").fetchone()
            assert row is not None, "the migrated db has a thread row to duplicate"
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO threads (thread_hash, thread_id, from_id, to_id, "
                    "type, content, weaver, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (row["thread_hash"], "thread-dup-0001", row["from_id"],
                     row["to_id"], row["type"], row["content"], row["weaver"],
                     row["created_at"]))
        finally:
            c.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Byte-dup collapse + insert idempotency. RED until migrate_db lands.
# ══════════════════════════════════════════════════════════════════════════════

class TestByteDupCollapse:  # GREEN (impl 2): migrate_db landed
    def _inject_byte_dup(self, store, folio_id):
        """Two rows identical on all six canonical fields, differing only by
        thread_id — the exact case the PK collapse targets."""
        g = _mk_lineage(store, folio_id)
        c = _conn(store)
        try:
            ts = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
            h = compute_thread_hash(g, g, "reference", "w", ts, None)
            for tid in ("thread-bd-0001", "thread-bd-0002"):
                c.execute(
                    "INSERT INTO threads (thread_id, from_id, to_id, type, content, "
                    "weaver, created_at, thread_hash) VALUES (?,?,?,?,?,?,?,?)",
                    (tid, g, g, "reference", None, "w", ts, h))
            c.commit()
        finally:
            c.close()
        return h

    def test_byte_duplicates_collapse_to_one(self, store):
        _make_site(store)
        h = self._inject_byte_dup(store, "finding-20260703-e001")
        before = [r for r in _rows(store) if r["thread_hash"] == h]
        assert len(before) == 2, "two byte-identical rows pre-swap"
        result = _load_pk_migration().migrate_db(_db(store))
        after = [r for r in _rows(store) if r["thread_hash"] == h]
        assert len(after) == 1, "collapsed to one under the PK"
        assert h in {d.get("thread_hash") if isinstance(d, dict) else d
                     for d in result.collapsed}, "the collapse is logged"

    def test_distinct_created_at_both_survive(self, store):
        # §4.1: content-addressing includes created_at, so re-asserting the same
        # logical state at a different instant is NOT collapsed.
        _make_site(store)
        g = _mk_lineage(store, "finding-20260703-e002")
        t0 = datetime(2026, 2, 2, tzinfo=timezone.utc)
        _post_thread(store, g, g, "reference", created_at=t0)
        _post_thread(store, g, g, "reference", created_at=t0 + timedelta(seconds=1))
        _load_pk_migration().migrate_db(_db(store))
        refs = [r for r in _rows(store, "reference")]
        assert len(refs) == 2, "distinct created_at -> distinct hash -> both kept"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Class B re-anchor + thread_hash recompute. RED until migrate_db lands.
# ══════════════════════════════════════════════════════════════════════════════

class TestClassBReanchor:  # GREEN (impl 2): migrate_db landed
    def test_reference_reanchored_to_genesis_and_hash_recomputed(self, store):
        _make_site(store)
        ga = _mk_lineage(store, "finding-20260703-f0a1")
        gb = _mk_lineage(store, "finding-20260703-f0b1")
        _post_thread(store, "finding-20260703-f0a1", "finding-20260703-f0b1",
                     "reference", weaver="w", content=None)
        # pre-swap: slug-keyed
        pre = _rows(store, "reference")[0]
        assert pre["from_id"] == "finding-20260703-f0a1"

        _load_pk_migration().migrate_db(_db(store))

        post = _rows(store, "reference")[0]
        assert post["from_id"] == ga and post["to_id"] == gb, "re-keyed to genesis"
        assert post["thread_hash"] == compute_thread_hash(
            ga, gb, "reference", post["weaver"], post["created_at"], post["content"]
        ), "thread_hash recomputed over the new endpoints"

    def test_version_anchored_supersedes_not_reanchored(self, store):
        # fell:phase3b-design:r2:type-gate — supersedes endpoints are version hashes;
        # they must NOT be slug-ified even if head==genesis.
        _make_site(store)
        _mk_lineage(store, "finding-20260703-f0c1", "v1")
        _edit(store, "finding-20260703-f0c1", "v2")  # mints a supersedes edge
        pre = _rows(store, "supersedes")[0]
        _load_pk_migration().migrate_db(_db(store))
        post = _rows(store, "supersedes")[0]
        assert (post["from_id"], post["to_id"]) == (pre["from_id"], pre["to_id"]), \
            "version-anchored edge left byte-faithful"

    def test_every_post_row_thread_hash_self_verifies(self, store):
        _make_site(store)
        ga = _mk_lineage(store, "finding-20260703-f0d1")
        gb = _mk_lineage(store, "finding-20260703-f0e1")
        _post_thread(store, "finding-20260703-f0d1", "finding-20260703-f0e1", "reference")
        _edit(store, "finding-20260703-f0d1", "v2")
        _load_pk_migration().migrate_db(_db(store))
        for r in _rows(store):
            assert r["thread_hash"] == compute_thread_hash(
                r["from_id"], r["to_id"], r["type"], r["weaver"],
                r["created_at"], r["content"]), f"row {r['thread_id']} self-verifies"


class TestI1AndCollisionRefusal:  # GREEN (impl 2): migrate_db landed
    def _force_i1_collision(self, store, slug_a, slug_b):
        """Point two refs at the SAME genesis_hash — an I1 violation."""
        ga = _mk_lineage(store, slug_a)
        _mk_lineage(store, slug_b)
        c = _conn(store)
        try:
            c.execute("UPDATE refs SET genesis_hash=? WHERE slug=?", (ga, slug_b))
            c.commit()
        finally:
            c.close()

    def test_i1_collision_refuses(self, store):
        # fell:phase3b-design:r1:i1-precondition — the whole re-anchor rests on I1;
        # a violation is a typed refusal, never a silent collapse.
        _make_site(store)
        self._force_i1_collision(store, "finding-20260703-g0a1", "finding-20260703-g0b1")
        m = _load_pk_migration()
        with pytest.raises(m.PreconditionError):
            m.migrate_db(_db(store))

    def test_reanchor_collision_is_refused_not_collapsed(self, store):
        # fell:phase3b-design:r1:collision-blocker — two DISTINCT reference edges that
        # would collapse post-anchor (only possible under an I1 violation) must be a
        # BLOCKER, never a benign drop of a non-regenerable edge (§5.3).
        _make_site(store)
        self._force_i1_collision(store, "finding-20260703-g0c1", "finding-20260703-g0d1")
        # two references from each colliding lineage to a common target
        gt = _mk_lineage(store, "finding-20260703-g0t1")
        _post_thread(store, "finding-20260703-g0c1", "finding-20260703-g0t1", "reference")
        _post_thread(store, "finding-20260703-g0d1", "finding-20260703-g0t1", "reference")
        m = _load_pk_migration()
        with pytest.raises(m.PreconditionError):
            m.migrate_db(_db(store))

    def test_null_thread_id_refused_not_silently_dropped(self, store):
        # fell:phase3b-impl:r1:null-thread-id (codex) — SQLite does NOT enforce
        # NOT-NULL on a non-INTEGER PRIMARY KEY, so the legacy thread_id TEXT PK can
        # hold a NULL. threads_new.thread_id is NOT NULL, so a bare copy would silently
        # swallow such a row (row loss NOT in result.collapsed). Refuse the db instead.
        _make_site(store)
        g = _mk_lineage(store, "finding-20260703-g0n1")
        c = _conn(store)
        try:
            ts = datetime(2026, 3, 3, tzinfo=timezone.utc).isoformat()
            h = compute_thread_hash(g, g, "reference", "w", ts, None)
            c.execute(
                "INSERT INTO threads (thread_id, from_id, to_id, type, content, "
                "weaver, created_at, thread_hash) VALUES (NULL,?,?,?,?,?,?,?)",
                (g, g, "reference", None, "w", ts, h))
            c.commit()
        finally:
            c.close()
        before = _rows(store)
        m = _load_pk_migration()
        with pytest.raises(m.PreconditionError):
            m.migrate_db(_db(store))
        assert _rows(store) == before, "a refused db is left byte-untouched"


# ══════════════════════════════════════════════════════════════════════════════
# 5. The per-row B/C classifier + manifest predicate. RED until the module lands.
# ══════════════════════════════════════════════════════════════════════════════

class TestBCClassification:  # GREEN (impl 1): classify_row landed
    def _sets(self):
        slugs = {"finding-a", "finding-b"}
        versions = {"sha256::ga", "sha256::gb"}
        return slugs, versions

    def test_control_is_class_a(self):
        m = _load_pk_migration()
        slugs, versions = self._sets()
        row = {"from_id": "finding-a", "to_id": "finding-a", "type": "status"}
        assert m.classify_row(row, slugs=slugs, versions=versions) == "A"

    def test_reference_folio_to_folio_is_b(self):
        m = _load_pk_migration()
        slugs, versions = self._sets()
        row = {"from_id": "finding-a", "to_id": "finding-b", "type": "reference"}
        assert m.classify_row(row, slugs=slugs, versions=versions) == "B"

    def test_reference_self_loop_is_c(self):
        # fell:phase3b-design:r3:reference-self-loop
        m = _load_pk_migration()
        slugs, versions = self._sets()
        row = {"from_id": "finding-a", "to_id": "finding-a", "type": "reference"}
        assert m.classify_row(row, slugs=slugs, versions=versions) == "C"

    @pytest.mark.parametrize("ttype", ["tag", "message"])
    def test_tag_and_message_are_c_even_when_folio_to_folio(self, ttype):
        # fell:phase3b-design:r1:message-override + tag override (§5)
        m = _load_pk_migration()
        slugs, versions = self._sets()
        row = {"from_id": "finding-a", "to_id": "finding-b", "type": ttype}
        assert m.classify_row(row, slugs=slugs, versions=versions) == "C"

    def test_orphan_endpoint_is_c(self):
        m = _load_pk_migration()
        slugs, versions = self._sets()
        row = {"from_id": "finding-a", "to_id": "deleted-999", "type": "reference"}
        assert m.classify_row(row, slugs=slugs, versions=versions) == "C"

    def test_unknown_type_between_folios_is_c(self):
        # fell:phase3b-impl:r1:classify-allowlist (opus note) — the B decision is an
        # allow-list; an unknown/new type (even distinct folio->folio) falls to C, so
        # migrate_db never silently re-anchors a row of an unrecognized type.
        m = _load_pk_migration()
        slugs, versions = self._sets()
        row = {"from_id": "finding-a", "to_id": "finding-b", "type": "somenewtype"}
        assert m.classify_row(row, slugs=slugs, versions=versions) == "C"


class TestManifestPredicate:  # GREEN (impl 1): manifest_eligible landed
    def _versions(self):
        return {"sha256::ga", "sha256::gb"}

    def test_reference_between_folios_is_eligible(self):
        m = _load_pk_migration()
        row = {"from_id": "sha256::ga", "to_id": "sha256::gb", "type": "reference"}
        assert m.manifest_eligible(row, versions=self._versions()) is True

    def test_distinct_endpoint_control_excluded_locks_type_gate(self):
        # fell:phase3b-design:r1:manifest-predicate-leak — the type gate, isolated:
        # DISTINCT version-hash endpoints (so from!=to does NOT exclude it) with a
        # control type must still be rejected. A self-loop (ga->ga) would pass on the
        # from!=to gate alone and not prove the type gate fires; this does.
        m = _load_pk_migration()
        row = {"from_id": "sha256::ga", "to_id": "sha256::gb", "type": "assignment"}
        assert m.manifest_eligible(row, versions=self._versions()) is False

    def test_unknown_type_excluded_locks_allowlist(self):
        # The predicate is an ALLOW-LIST (§7): a NEW/unknown type between two folios
        # must be rejected, so a denylist impl (which would federate it) fails here.
        m = _load_pk_migration()
        row = {"from_id": "sha256::ga", "to_id": "sha256::gb", "type": "somenewtype"}
        assert m.manifest_eligible(row, versions=self._versions()) is False

    @pytest.mark.parametrize("ttype", ["tag", "message"])
    def test_tag_message_excluded(self, ttype):
        m = _load_pk_migration()
        row = {"from_id": "sha256::ga", "to_id": "sha256::gb", "type": ttype}
        assert m.manifest_eligible(row, versions=self._versions()) is False

    def test_self_loop_excluded_locks_fromto_gate(self):
        # The from!=to gate, isolated: a B-eligible TYPE (reference) as a self-loop
        # must be rejected (a type-gate-only impl would admit it).
        m = _load_pk_migration()
        row = {"from_id": "sha256::ga", "to_id": "sha256::ga", "type": "reference"}
        assert m.manifest_eligible(row, versions=self._versions()) is False

    def test_endpoint_not_in_versions_excluded(self):
        # Both endpoints must resolve in versions; a slug-keyed (unresolved) endpoint
        # is not manifest-eligible.
        m = _load_pk_migration()
        row = {"from_id": "sha256::ga", "to_id": "finding-b", "type": "reference"}
        assert m.manifest_eligible(row, versions=self._versions()) is False

    def test_supersedes_eligible(self):
        m = _load_pk_migration()
        row = {"from_id": "sha256::gb", "to_id": "sha256::ga", "type": "supersedes"}
        assert m.manifest_eligible(row, versions=self._versions()) is True


# ══════════════════════════════════════════════════════════════════════════════
# 6. The §6.1 read-view — GET /threads//search unchanged across the swap.
#    Asserted against the INDEPENDENT shadow, never reader==reader.
# ══════════════════════════════════════════════════════════════════════════════

class TestClassBReadViewPreserved:  # GREEN (impl 3): get_threads_display extended
    def test_reference_edge_still_queryable_by_slug_after_swap(self, store):
        # fell:phase3b-design:r2:classb-read-view — the load-bearing one: re-anchoring
        # slug->genesis must NOT drop a Class B edge from a slug query, and must
        # display it slug-keyed. get_threads_display is extended for the
        # genesis-anchored Class B types (§6.1).
        _make_site(store)
        _mk_lineage(store, "finding-20260703-h0a1")
        _mk_lineage(store, "finding-20260703-h0b1")
        _post_thread(store, "finding-20260703-h0a1", "finding-20260703-h0b1", "reference")

        before = _display_via_reader(store, from_id="finding-20260703-h0a1")
        _load_pk_migration().migrate_db(_db(store))
        after = _display_via_reader(store, from_id="finding-20260703-h0a1")

        assert after == before, "GET /threads?from_id=slug byte-identical across swap"
        # and it equals the INDEPENDENT expectation, not just its own pre-image
        expected = _expected_display(_rows(store), _refs_by_slug(store),
                                     from_id="finding-20260703-h0a1")
        assert after == expected, "reader matches the de-circularized shadow"

    def test_fetch_all_shows_reference_slug_keyed(self, store):
        _make_site(store)
        _mk_lineage(store, "finding-20260703-h0c1")
        _mk_lineage(store, "finding-20260703-h0d1")
        _post_thread(store, "finding-20260703-h0c1", "finding-20260703-h0d1", "reference")
        _load_pk_migration().migrate_db(_db(store))
        got = _display_via_reader(store)
        expected = _expected_display(_rows(store), _refs_by_slug(store))
        assert got == expected
        assert ("finding-20260703-h0c1", "finding-20260703-h0d1", "reference", None) in got

    def test_supersedes_stays_hash_keyed_in_display(self, store):
        _make_site(store)
        _mk_lineage(store, "finding-20260703-h0e1", "v1")
        _edit(store, "finding-20260703-h0e1", "v2")
        _load_pk_migration().migrate_db(_db(store))
        got = _display_via_reader(store, type="supersedes")
        assert got, "supersedes surfaces in display"
        for f, t, ty, _c in got:
            assert f.startswith("sha256::") and t.startswith("sha256::"), \
                "version-anchored edge shown byte-faithful (hash endpoints)"


# ══════════════════════════════════════════════════════════════════════════════
# 7. The differential verifier — positive path. RED until verify_db lands.
#    Clean-path proves nothing; inject each defect and assert it BLOCKS
#    (the lesson from the A5-cleanup fell).
# ══════════════════════════════════════════════════════════════════════════════

class TestVerifierPositivePath:  # GREEN (impl 4): verify_db landed
    def _migrated(self, store, folio_id="finding-20260703-j001"):
        # TWO lineages (so the post-I1 gate has two refs to collide) + a re-anchored
        # Class B reference (so the clean-path covers a re-keyed row) + an edit (a
        # supersedes row for the dangling test to target).
        _make_site(store)
        _mk_lineage(store, folio_id, "v1")
        _mk_lineage(store, "finding-20260703-j002", "v1")
        _post_thread(store, folio_id, "finding-20260703-j002", "reference")
        _edit(store, folio_id, "v2")
        _load_pk_migration().migrate_db(_db(store))
        return _load_pk_verifier()

    def test_clean_migrated_db_has_no_problems(self, store):
        v = self._migrated(store)
        problems, _ = v.verify_db(_db(store))
        assert not problems, f"a clean post-swap db verifies: {problems}"

    def test_blocks_on_non_self_verifying_hash(self, store):
        v = self._migrated(store)
        c = _conn(store)
        try:
            c.execute("UPDATE threads SET content='tampered' "
                      "WHERE thread_hash=(SELECT thread_hash FROM threads LIMIT 1)")
            c.commit()
        finally:
            c.close()
        problems, _ = v.verify_db(_db(store))
        assert any("self-verify" in p for p in problems), problems

    def test_blocks_on_dangling_endpoint(self, store):
        # Isolate the dangling gate: move an endpoint to a hash NOT in versions AND
        # recompute thread_hash to MATCH — so self-verify PASSES and only a real
        # dangling-endpoint check can fire (else this just re-proves self-verify).
        v = self._migrated(store)
        c = _conn(store)
        try:
            row = c.execute(
                "SELECT * FROM threads WHERE type='supersedes' LIMIT 1").fetchone()
            new_from = "sha256::00000000notinversions"
            new_hash = compute_thread_hash(
                new_from, row["to_id"], row["type"], row["weaver"],
                row["created_at"], row["content"])
            c.execute("UPDATE threads SET from_id=?, thread_hash=? WHERE thread_id=?",
                      (new_from, new_hash, row["thread_id"]))
            c.commit()
        finally:
            c.close()
        problems, _ = v.verify_db(_db(store))
        assert any("dangl" in p.lower() or "not in versions" in p.lower()
                   for p in problems), \
            f"the dangling gate (not self-verify) must fire: {problems}"

    def test_blocks_on_surviving_duplicate_thread_hash(self, store):
        # design §8: a surviving duplicate thread_hash is a BLOCKER. Under a real PK
        # a dup can't be inserted, so destroy the PK (rebuild without it) + inject a
        # dup — the verifier must catch that thread_hash is not actually unique/PK.
        v = self._migrated(store)
        c = _conn(store)
        try:
            cols = ("thread_hash, thread_id, from_id, to_id, type, content, "
                    "weaver, created_at")
            c.execute("ALTER TABLE threads RENAME TO threads_old")
            c.execute(f"CREATE TABLE threads ({cols.replace(', ', ' TEXT, ')} TEXT)")
            c.execute(f"INSERT INTO threads ({cols}) SELECT {cols} FROM threads_old")
            row = c.execute("SELECT * FROM threads_old LIMIT 1").fetchone()
            c.execute(
                f"INSERT INTO threads ({cols}) VALUES (?,?,?,?,?,?,?,?)",
                (row["thread_hash"], "thread-dup-xxxx", row["from_id"], row["to_id"],
                 row["type"], row["content"], row["weaver"], row["created_at"]))
            c.execute("DROP TABLE threads_old")
            c.commit()
        finally:
            c.close()
        problems, _ = v.verify_db(_db(store))
        assert any("uniqu" in p.lower() or "duplicate" in p.lower()
                   or "primary key" in p.lower()
                   for p in problems), \
            f"a surviving duplicate thread_hash must block: {problems}"

    def test_blocks_on_post_i1_violation(self, store):
        # I1 defense-in-depth: even post-swap the verifier confirms genesis is unique
        # per lineage (inject a shared genesis). _migrated builds two lineages, so
        # this ASSERTS (never skips) — a skip here would leave the I1 gate unexercised.
        v = self._migrated(store)
        c = _conn(store)
        try:
            rows = c.execute("SELECT slug, genesis_hash FROM refs").fetchall()
            assert len(rows) >= 2, "fixture must provide two refs to force I1"
            c.execute("UPDATE refs SET genesis_hash=? WHERE slug=?",
                      (rows[0]["genesis_hash"], rows[1]["slug"]))
            c.commit()
        finally:
            c.close()
        problems, _ = v.verify_db(_db(store))
        assert any("i1" in p.lower() or "genesis" in p.lower() for p in problems), \
            f"a post-swap I1 violation must block: {problems}"

    def test_blocks_on_missing_index(self, store):
        # fell:phase3b-design:r3:index-presence — an unindexed hot table passes every
        # value check; the verifier must catch it.
        v = self._migrated(store)
        c = _conn(store)
        try:
            c.execute("DROP INDEX IF EXISTS idx_threads_from")
            c.commit()
        finally:
            c.close()
        problems, _ = v.verify_db(_db(store))
        assert any("idx_threads_from" in p or "index" in p.lower() for p in problems), \
            problems


# ══════════════════════════════════════════════════════════════════════════════
# 8. Differential equivalence + migration invariants. The equivalence legs are
#    before/after SPEC comparisons (the migration harness holds the pre-image),
#    not verify_db legs.
# ══════════════════════════════════════════════════════════════════════════════

class TestControlReadUnchanged:  # GREEN (impl 2): migrate_db landed
    """The other §8 equivalence surface: control read identical across the swap.
    The rewrite touches every row (incl. 5283 status rows ecosystem-wide); a reorder
    or drop must be caught, and 'thread_id is kept so it's safe' is an argument, not
    a test."""

    def test_status_and_assignment_reads_identical_across_swap(self, store):
        _make_site(store)
        _mk_lineage(store, "finding-20260703-k001")
        _post_thread(store, "finding-20260703-k001", "finding-20260703-k001",
                     "status", content="closed")
        _post_thread(store, "finding-20260703-k001", "agent-x", "assignment")

        before_s = store.get_latest_statuses(["finding-20260703-k001"])
        before_a = store.get_latest_assignments(["finding-20260703-k001"])
        assert before_s == {"finding-20260703-k001": "closed"}

        _load_pk_migration().migrate_db(_db(store))

        assert store.get_latest_statuses(["finding-20260703-k001"]) == before_s, \
            "status read unchanged across the swap"
        assert store.get_latest_assignments(["finding-20260703-k001"]) == before_a, \
            "assignment read unchanged across the swap"


class TestMigrationInvariants:  # GREEN (impl 2): migrate_db landed
    def test_row_count_reconciles_to_logged_collapse_only(self, store):
        _make_site(store)
        ga = _mk_lineage(store, "finding-20260703-l001")
        _mk_lineage(store, "finding-20260703-l002")
        _post_thread(store, "finding-20260703-l001", "finding-20260703-l002", "reference")
        before = len(_rows(store))
        result = _load_pk_migration().migrate_db(_db(store))
        after = len(_rows(store))
        assert after == before - len(result.collapsed), \
            "row count moves by exactly the logged byte-dup collapse, nothing else"

    def test_second_run_is_noop_not_refused(self, store):
        # fell:phase3b-design:r1:idempotent-vs-backup — an already-migrated db
        # short-circuits BEFORE the no-prior-backup precondition, so a re-run neither
        # refuses nor rewrites.
        _make_site(store)
        _mk_lineage(store, "finding-20260703-l003")
        m = _load_pk_migration()
        m.migrate_db(_db(store))
        r2 = m.migrate_db(_db(store))  # must not raise PreconditionError
        assert r2.reanchored == 0 and not r2.collapsed, "second run is a no-op"

    def test_dry_run_mutates_nothing(self, store):
        _make_site(store)
        _mk_lineage(store, "finding-20260703-l004")
        _mk_lineage(store, "finding-20260703-l005")
        _post_thread(store, "finding-20260703-l004", "finding-20260703-l005", "reference")
        before = sorted(_rows(store), key=lambda r: r["thread_id"])
        _load_pk_migration().migrate_db(_db(store), dry_run=True)
        after = sorted(_rows(store), key=lambda r: r["thread_id"])
        assert after == before, "dry_run leaves the threads table byte-identical"

    def test_orphan_reference_kept_and_logged_not_dropped(self, store):
        # fell:phase3b-design:r1:orphan-keep-log (§5.3) — structural edges are not
        # regenerable, so an orphan endpoint is KEPT (slug-keyed) and logged, never
        # dropped like a 3a control orphan. This is migration BEHAVIOR, not classify.
        _make_site(store)
        _mk_lineage(store, "finding-20260703-l006")
        _post_thread(store, "finding-20260703-l006", "deleted-00000000-zzzz", "reference")
        before = len(_rows(store, "reference"))
        result = _load_pk_migration().migrate_db(_db(store))
        after_rows = _rows(store, "reference")
        assert len(after_rows) == before, "orphan reference kept, not dropped"
        orphan = [r for r in after_rows if r["to_id"] == "deleted-00000000-zzzz"]
        assert orphan, "the orphan row survives"
        assert orphan[0]["from_id"] == "finding-20260703-l006", \
            "orphan stays slug-keyed (not re-anchored)"
        assert result.orphans, "the kept orphan is logged"

    def test_reverted_marker_survives_swap(self, store):
        # §4.3 — the reverted marker is the one intentional per-event row; distinct
        # created_at -> distinct hash, so it survives the collapse.
        _make_site(store)
        _mk_lineage(store, "finding-20260703-l007", "v1")
        _edit(store, "finding-20260703-l007", "v2")
        f3 = store.get_folio("finding-20260703-l007")
        f3.content = "v1"
        store.save_folio(f3, editor="e")  # revert -> mints a 'reverted' marker
        assert len(_rows(store, "reverted")) == 1
        _load_pk_migration().migrate_db(_db(store))
        assert len(_rows(store, "reverted")) == 1, "reverted marker survives the swap"

    def test_class_c_orphan_reference_stays_slug_keyed_in_display(self, store):
        # codex r1: the display shadow rewrites by type; lock that a Class C
        # (orphan) reference is NOT slugified/dropped — it stays as posted.
        _make_site(store)
        _mk_lineage(store, "finding-20260703-l008")
        _post_thread(store, "finding-20260703-l008", "deleted-00000000-yyyy", "reference")
        _load_pk_migration().migrate_db(_db(store))
        got = _display_via_reader(store, from_id="finding-20260703-l008")
        expected = _expected_display(_rows(store), _refs_by_slug(store),
                                     from_id="finding-20260703-l008")
        assert got == expected
        assert ("finding-20260703-l008", "deleted-00000000-yyyy", "reference", None) in got
