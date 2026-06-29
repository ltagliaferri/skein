"""Seeded multi-version conformance gate for Phase 1+2 (versions/refs + edit-as-commit).

The fidelity harness is structurally BLIND to head-filtering: its fixture is
frozen edit-free data, so every lineage has exactly one version (versions ==
heads) and a read that omits the head filter returns identical output. This test
is the REAL gate for the read-flip (commit C): it SEEDS multiple versions per
slug and asserts every read returns ONLY the head, while superseded content stays
addressable by hash forever.

It is written to pass at BOTH commit B (reads still hit folios, which only ever
holds current content) and commit C (reads hit the versions⋈refs heads join) — so
it pins the contract across the read-flip rather than re-blessing through it. The
internal-table assertions (versions/refs/threads shape) hold from commit B on.
"""

import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skein import storage as storage_mod
from skein.models import Folio, Site
from skein.storage import (
    JSONStore,
    get_project_last_activity_timestamps,
    resolve_folio_across_projects,
    search_folio_across_projects,
)


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d)


@pytest.fixture
def store(tmp_dir):
    return JSONStore(tmp_dir)


def _conn(store):
    c = sqlite3.connect(store.base_dir / "skein.db")
    c.row_factory = sqlite3.Row
    return c


def _make_site(store, site_id="alpha"):
    store.save_site(Site(
        site_id=site_id,
        created_at=datetime.now(timezone.utc),
        created_by="tester",
        purpose="conformance",
    ))


def _folio(folio_id, content, *, site_id="alpha", created_at=None,
           created_by="author", type="finding", title="T",
           status="open", assigned_to=None, target_agent=None, metadata=None):
    return Folio(
        folio_id=folio_id,
        type=type,
        site_id=site_id,
        created_at=created_at or datetime.now(timezone.utc),
        created_by=created_by,
        title=title,
        content=content,
        status=status,
        assigned_to=assigned_to,
        target_agent=target_agent,
        metadata=metadata or {},
    )


def _counts(store):
    c = _conn(store)
    try:
        v = c.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        r = c.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        sup = c.execute("SELECT COUNT(*) FROM threads WHERE type='supersedes'").fetchone()[0]
        rev = c.execute("SELECT COUNT(*) FROM threads WHERE type='reverted'").fetchone()[0]
        return {"versions": v, "refs": r, "supersedes": sup, "reverted": rev}
    finally:
        c.close()


def _ref(store, slug):
    c = _conn(store)
    try:
        return c.execute("SELECT * FROM refs WHERE slug=?", (slug,)).fetchone()
    finally:
        c.close()


# ── 1+2. mint v1, edit to v2; every read returns only the head ────────────────

def test_edit_mints_version_and_moves_head(store):
    _make_site(store)
    f = _folio("finding-20260629-aaaa", "genesis body")
    store.save_folio(f)

    after_create = _counts(store)
    assert after_create == {"versions": 1, "refs": 1, "supersedes": 0, "reverted": 0}
    ref1 = _ref(store, "finding-20260629-aaaa")
    assert ref1["head_hash"] == ref1["genesis_hash"]
    v1_hash = ref1["head_hash"]

    # Edit the content (route fetches current, mutates, re-saves with editor).
    f2 = store.get_folio("finding-20260629-aaaa")
    f2.content = "edited body v2"
    store.save_folio(f2, editor="editor-agent")

    after_edit = _counts(store)
    assert after_edit == {"versions": 2, "refs": 1, "supersedes": 1, "reverted": 0}

    ref2 = _ref(store, "finding-20260629-aaaa")
    assert ref2["genesis_hash"] == v1_hash, "genesis is immutable"
    assert ref2["head_hash"] != v1_hash, "head moved to v2"
    v2_hash = ref2["head_hash"]

    # supersedes edge: new->old
    c = _conn(store)
    try:
        edge = c.execute("SELECT * FROM threads WHERE type='supersedes'").fetchone()
        assert edge["from_id"] == v2_hash and edge["to_id"] == v1_hash
        assert edge["weaver"] == "editor-agent"
    finally:
        c.close()

    # Every content read returns ONLY the head (v2). One lineage => one row.
    got = store.get_folio("finding-20260629-aaaa")
    assert got.content == "edited body v2"
    assert store._log_db.get_folio_count() == 1
    assert len(store.get_folios(site_id="alpha")) == 1
    stats = store._log_db.get_folio_stats()
    assert stats["total"] == 1
    activity = store._log_db.get_site_last_activity()
    assert set(activity.keys()) == {"alpha"}

    # Superseded v1 stays in versions, byte-identical.
    c = _conn(store)
    try:
        v1 = c.execute("SELECT * FROM versions WHERE content_hash=?", (v1_hash,)).fetchone()
        assert v1 is not None and v1["content"] == "genesis body"
        # created_at/created_by inherit genesis on v2.
        v2 = c.execute("SELECT * FROM versions WHERE content_hash=?", (v2_hash,)).fetchone()
        assert v2["created_by"] == v1["created_by"]
        assert v2["created_at"] == v1["created_at"]
    finally:
        c.close()


# ── search (FTS) + stats return heads only, never superseded content ──────────

def test_search_and_stats_return_heads_only(store):
    _make_site(store)
    f = _folio("finding-20260629-5555", "uniquegenesisword body", type="issue")
    store.save_folio(f)
    # Edit content so v1's unique word ("uniquegenesisword") is no longer the head.
    f2 = store.get_folio("finding-20260629-5555")
    f2.content = "uniqueeditedword body"
    store.save_folio(f2, editor="e")

    # FTS: a word only in the SUPERSEDED v1 must NOT surface (head-only). A word in
    # the head must. (At commit B folios_fts holds v2 only; at C versions_fts holds
    # both and the head join filters v1 — this pins the contract across the flip.)
    assert store.search_folios("uniquegenesisword") == []
    hits = store.search_folios("uniqueeditedword")
    assert [h.folio_id for h in hits] == ["finding-20260629-5555"]

    # Stats count lineages (heads), not versions.
    stats = store._log_db.get_folio_stats()
    assert stats["total"] == 1
    assert stats["by_type"].get("issue") == 1
    assert sum(stats["by_status"].values()) == 1


def test_search_surface_is_prose_not_slug(store):
    # CONSCIOUS surface change at the read-flip: folios_fts indexed
    # folio_id+title+content; versions_fts (heads) indexes content_hash+title+content.
    # FTS search is now over PROSE (title/content), not the slug. Slug-token search
    # via FTS was an accidental side-effect of indexing folio_id; exact-slug lookup
    # lives in get_folio and slug/type/date filtering lives in `find` (get_folios).
    # This test pins the surface so the change stays conscious, not silent.
    _make_site(store)
    # folio_id carries a token ("zqxw9") that appears in NEITHER title nor content.
    f = _folio("finding-20260629-zqxw9", "Beta gamma delta", title="Alpha")
    store.save_folio(f)
    assert [h.folio_id for h in store.search_folios("gamma")] == ["finding-20260629-zqxw9"]
    assert [h.folio_id for h in store.search_folios("Alpha")] == ["finding-20260629-zqxw9"]
    assert store.search_folios("zqxw9") == [], "slug token is not an FTS surface post-flip"
    # KNOWN ARTIFACT (conscious, pinned): versions_fts also indexes content_hash
    # (design §2.2), so the literal token "sha256" matches every head (every
    # content_hash is sha256::<hex>). Harmless — hash lookup is the by-hash resolver
    # (commit D) / the join, never FTS. Dropping content_hash from the index would
    # be cleaner but risks BM25 doc-length drift vs the blessed harness baseline;
    # candidate for a Phase 3 FTS cleanup. Pinned here so the surface stays conscious.
    assert len(store.search_folios("sha256")) >= 1


# ── 3. created_at/created_by inherit genesis even if the editor hands fresh ───

def test_genesis_fields_inherited_on_edit(store):
    _make_site(store)
    genesis_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    f = _folio("finding-20260629-bbbb", "g", created_at=genesis_at, created_by="alice")
    store.save_folio(f)
    gen_hash = _ref(store, "finding-20260629-bbbb")["head_hash"]

    # Hand an edit with a DIFFERENT created_at/created_by — must not leak into the
    # version PK; genesis is reasserted before the recompute.
    f2 = store.get_folio("finding-20260629-bbbb")
    f2.content = "edited"
    f2.created_at = datetime(2026, 6, 6, tzinfo=timezone.utc)
    f2.created_by = "mallory"
    store.save_folio(f2, editor="mallory")

    c = _conn(store)
    try:
        for v in c.execute("SELECT * FROM versions").fetchall():
            assert v["created_by"] == "alice"
            # PK must verify against its own columns.
            from skein.identity import compute_folio_hash
            assert compute_folio_hash({
                "type": v["type"], "title": v["title"], "content": v["content"],
                "created_at": v["created_at"], "created_by": v["created_by"],
            }) == v["content_hash"]
    finally:
        c.close()
    # genesis hash unchanged.
    assert _ref(store, "finding-20260629-bbbb")["genesis_hash"] == gen_hash


# ── status-only edit mints nothing, refreshes the control cache ───────────────

def test_status_only_edit_mints_no_version(store):
    _make_site(store)
    f = _folio("finding-20260629-cccc", "body")
    store.save_folio(f)
    before = _counts(store)

    # Status-only change (content untouched) — the route sets folio.status then saves.
    f2 = store.get_folio("finding-20260629-cccc")
    f2.status = "closed"
    store.save_folio(f2, editor="x")
    after = _counts(store)
    assert after == before, "no new version / edge for a status-only edit"
    assert _ref(store, "finding-20260629-cccc")["status"] == "closed", "control cache refreshed"


# ── 4. revert: head returns to a prior version, reverted marker, no cycle ──────

def test_revert_moves_head_and_marks_no_cycle(store):
    _make_site(store)
    f = _folio("finding-20260629-dddd", "v1")
    store.save_folio(f)
    v1_hash = _ref(store, "finding-20260629-dddd")["head_hash"]

    f2 = store.get_folio("finding-20260629-dddd")
    f2.content = "v2"
    store.save_folio(f2, editor="e")
    assert _counts(store)["versions"] == 2

    # Revert content back to v1.
    f3 = store.get_folio("finding-20260629-dddd")
    f3.content = "v1"
    store.save_folio(f3, editor="e")

    after = _counts(store)
    assert after["versions"] == 2, "no new version minted on revert"
    assert after["supersedes"] == 1, "no second supersedes edge (would cycle)"
    assert after["reverted"] == 1, "a durable reverted marker exists"
    assert _ref(store, "finding-20260629-dddd")["head_hash"] == v1_hash, "head back to v1"
    assert store.get_folio("finding-20260629-dddd").content == "v1"


# ── 5. dedup: two lineages reach identical content, share one version row ──────

def test_dedup_two_refs_share_one_version(store):
    _make_site(store)
    shared_at = datetime(2026, 2, 2, tzinfo=timezone.utc)
    # Identical five identity fields => identical hash.
    a = _folio("finding-20260629-e001", "same", created_at=shared_at, created_by="auth", title="ID")
    b = _folio("finding-20260629-e002", "same", created_at=shared_at, created_by="auth", title="ID")
    store.save_folio(a)
    store.save_folio(b)

    c = _conn(store)
    try:
        assert c.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1, "one shared version row"
        assert c.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 2, "two independent refs"
        ha = _ref(store, "finding-20260629-e001")["head_hash"]
        hb = _ref(store, "finding-20260629-e002")["head_hash"]
        assert ha == hb, "both heads point at the shared version"
    finally:
        c.close()
    # Each ref resolves its own head independently.
    assert store.get_folio("finding-20260629-e001").content == "same"
    assert store.get_folio("finding-20260629-e002").content == "same"


# ── cross-lineage dedup edit is FORWARD, not a revert (lineage discriminator) ──

def test_cross_lineage_dedup_edit_is_forward_not_revert(store):
    _make_site(store)
    shared_at = datetime(2026, 3, 3, tzinfo=timezone.utc)
    # Lineage B already holds a version with content "target".
    b = _folio("finding-20260629-b001", "target", created_at=shared_at,
               created_by="auth", title="ID")
    store.save_folio(b)
    # Lineage A starts at different content, then is edited TO B's content. Because
    # A and B share created_at/created_by/title/type, A's edited hash dedups onto
    # B's existing version — but for A this is a FORWARD edit, not a revert.
    a = _folio("finding-20260629-a001", "origin", created_at=shared_at,
               created_by="auth", title="ID")
    store.save_folio(a)
    a2 = store.get_folio("finding-20260629-a001")
    a2.content = "target"
    store.save_folio(a2, editor="ed")

    c = _conn(store)
    try:
        # One shared version row for "target" (dedup); A's origin + B/A's target = 2.
        assert c.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 2
        # A forward edit => a supersedes edge for A, and NO bogus reverted marker.
        sup = c.execute("SELECT COUNT(*) FROM threads WHERE type='supersedes'").fetchone()[0]
        rev = c.execute("SELECT COUNT(*) FROM threads WHERE type='reverted'").fetchone()[0]
        assert sup == 1, "A's forward edit wrote a supersedes edge"
        assert rev == 0, "a cross-lineage dedup edit must NOT be marked a revert"
    finally:
        c.close()
    # A's head is the shared version; B unaffected.
    assert store.get_folio("finding-20260629-a001").content == "target"
    assert store.get_folio("finding-20260629-b001").content == "target"


# ── converge-then-diverge is handled SAFELY (no cycle), reads stay per-ref ─────

def test_converge_then_diverge_is_safe(store):
    # Two lineages converge on a shared version, then one diverges onto the other's
    # ancestor content. The global content DAG cannot hold this acyclically AND
    # per-genesis reachable; the code must stay safe (no cycle, no double-mint) and
    # reads must remain correct via the per-ref head. (A chain gap is accepted; it
    # is a verify WARNING, not a blocker.)
    from skein.migrations.verify_versions_refs import verify_db
    _make_site(store)
    at = datetime(2026, 4, 4, tzinfo=timezone.utc)
    common = dict(created_at=at, created_by="auth", title="ID", type="finding")

    b = _folio("finding-20260629-cv-b", "b0", **common)
    store.save_folio(b)
    a = _folio("finding-20260629-cv-a", "a0", **common)
    store.save_folio(a)
    # Both edit to the same shared content "shared" (A's edit dedups onto B's T).
    for slug in ("finding-20260629-cv-b", "finding-20260629-cv-a"):
        g = store.get_folio(slug)
        g.content = "shared"
        store.save_folio(g, editor="e")
    # B now diverges onto A's genesis content "a0".
    g = store.get_folio("finding-20260629-cv-b")
    g.content = "a0"
    store.save_folio(g, editor="e")

    # No crash; per-ref heads resolve independently and correctly.
    assert store.get_folio("finding-20260629-cv-b").content == "a0"
    assert store.get_folio("finding-20260629-cv-a").content == "shared"

    # The supersedes DAG is acyclic — verify reports NO blocker problems
    # (a chain-gap warning is acceptable).
    problems, _warnings = verify_db(store.base_dir / "skein.db")
    assert not problems, f"unexpected blocker problems: {problems}"


# ── 7. move_folio dual-writes site, mints no version, returns the new site ─────

def test_move_folio_dual_writes_site(store):
    _make_site(store, "alpha")
    _make_site(store, "beta")
    f = _folio("finding-20260629-ffff", "body", site_id="alpha")
    store.save_folio(f)
    head_before = _ref(store, "finding-20260629-ffff")["head_hash"]

    moved = store.move_folio("finding-20260629-ffff", "beta")
    assert moved.site_id == "beta"

    ref = _ref(store, "finding-20260629-ffff")
    c = _conn(store)
    try:
        frow = c.execute("SELECT site_id FROM folios WHERE folio_id=?",
                         ("finding-20260629-ffff",)).fetchone()
    finally:
        c.close()
    assert ref["site_id"] == "beta", "refs.site_id updated"
    assert frow["site_id"] == "beta", "folios.site_id updated (dual-write)"
    assert ref["head_hash"] == head_before, "move mints no new version"


# ── create-with-sugar: non-open status / assignee land in the genesis cache ───

def test_create_with_initial_status_seeds_refs_cache(store):
    _make_site(store)
    # Simulates the create-route fix: folio carries the true initial state BEFORE
    # the genesis save, so the refs cache is not seeded with 'open'/NULL.
    f = _folio("finding-20260629-9999", "body", status="in_progress", assigned_to="bob")
    store.save_folio(f)
    ref = _ref(store, "finding-20260629-9999")
    assert ref["status"] == "in_progress"
    assert ref["assigned_to"] == "bob"


# ── the three module-level raw readers flip to heads (cross-project surfaces) ──

def test_module_level_readers_return_heads(tmp_dir, monkeypatch):
    # A registered project with an edited folio; the raw cross-project readers must
    # resolve the HEAD (v2) via the join, never the superseded v1.
    data_dir = tmp_dir / "px" / ".skein" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = JSONStore(data_dir)
    _make_site(store)
    f = _folio("finding-20260629-mod1", "v1 body")
    store.save_folio(f)
    f2 = store.get_folio("finding-20260629-mod1")
    f2.content = "v2 body"
    store.save_folio(f2, editor="e")

    registry = {"px": {"path": str(tmp_dir / "px"), "data_dir": str(data_dir),
                       "name": "px"}}
    monkeypatch.setattr(storage_mod, "load_project_registry", lambda: registry)

    # search_folio_across_projects: existence via refs (head layer).
    found = search_folio_across_projects("finding-20260629-mod1", current_project_id=None)
    assert found and found["project_name"] == "px"
    assert search_folio_across_projects("nope-00000000-zzzz", None) is None

    # resolve_folio_across_projects: reconstructs the HEAD folio from the join.
    res = resolve_folio_across_projects("finding-20260629-mod1", current_project_id=None)
    assert res and res["folio"].content == "v2 body", "must resolve the head, not v1"

    # get_project_last_activity_timestamps: MAX(created_at) over heads.
    ts = get_project_last_activity_timestamps()
    assert "px" in ts and isinstance(ts["px"], int)


def test_module_readers_fall_back_to_folios_for_legacy_db(tmp_dir, monkeypatch):
    # A registered legacy db with ONLY folios (not yet backfilled, no refs table)
    # must still resolve cross-project — fall back to folios rather than silently
    # vanish. Steady state (all dbs backfilled via verify --all gate) never hits this.
    data_dir = tmp_dir / "legacy" / ".skein" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "skein.db"
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE folios (folio_id TEXT PRIMARY KEY, type TEXT, site_id TEXT, "
        "created_at TEXT, created_by TEXT, title TEXT, content TEXT, status TEXT, "
        "assigned_to TEXT, target_agent TEXT, omlet TEXT, archived INT, metadata TEXT, "
        "acknowledged_at TEXT, content_hash TEXT)"
    )
    c.execute(
        "INSERT INTO folios VALUES ('finding-legacy-0001','finding','s',"
        "'2026-01-01T00:00:00+00:00','a','T','legacy body','open',NULL,NULL,NULL,0,"
        "'{}',NULL,'sha256::x')"
    )
    c.commit()
    c.close()
    registry = {"legacy": {"path": str(tmp_dir / "legacy"), "data_dir": str(data_dir),
                           "name": "legacy"}}
    monkeypatch.setattr(storage_mod, "load_project_registry", lambda: registry)

    assert search_folio_across_projects("finding-legacy-0001", None) is not None
    res = resolve_folio_across_projects("finding-legacy-0001", None)
    assert res and res["folio"].content == "legacy body"
    assert "legacy" in get_project_last_activity_timestamps()


# ── target_agent / metadata survive a content edit through the read path ──────

def test_target_agent_and_metadata_survive_edit(store):
    _make_site(store)
    f = _folio("brief-20260629-7777", "body", type="brief",
               target_agent="agent-x", metadata={"priority": "high"})
    store.save_folio(f)

    f2 = store.get_folio("brief-20260629-7777")
    f2.content = "edited"
    store.save_folio(f2, editor="e")

    got = store.get_folio("brief-20260629-7777")
    assert got.target_agent == "agent-x"
    assert got.metadata.get("priority") == "high"
    # refs cache also carries them.
    ref = _ref(store, "brief-20260629-7777")
    assert ref["target_agent"] == "agent-x"
    assert '"priority": "high"' in (ref["metadata"] or "")
