"""Tests for the Phase 3a A5 Part 2 folios-drop driver (skein.migrations.drop_folios).

The driver is destructive (DROP TABLE folios on live dbs), so every gate is tested:
the precondition refuses each unsafe shape, dry-run never touches the live db, --live
backs up before dropping and refuses to bury a prior restore point, and verify catches
a drop that removed more than folios.

Fixture note (threads-only contraction, 2026-07-08): drop_folios is SPENT (A5 ran
ecosystem-wide 2026-07-03) and operates on the PRE-contraction refs schema — its
mirror checks read refs.status/assigned_to/archived, which the contraction dropped
from _init_db. ``_build`` therefore restores those legacy columns explicitly after
the storage layer creates the db (defaults match ``_folio``'s defaults), which is
exactly the on-disk shape of every db A5 actually ran against and of any pre-A5
backup it could ever run against again. The safety suite stays live this way
instead of being skipped (deep_code_audit 2026-07-08, finding 7).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from skein.identity import compute_folio_hash
from skein.migrations import drop_folios as df
from skein.models import Folio
from skein.storage import LogDatabase

UTC = timezone.utc

_LEGACY_FOLIOS_DDL = """
    CREATE TABLE IF NOT EXISTS folios (
        folio_id TEXT PRIMARY KEY, type TEXT NOT NULL, site_id TEXT NOT NULL,
        created_at DATETIME NOT NULL, created_by TEXT NOT NULL, title TEXT NOT NULL,
        content TEXT NOT NULL, status TEXT DEFAULT 'open', assigned_to TEXT,
        target_agent TEXT, omlet TEXT, archived INTEGER DEFAULT 0, metadata JSON,
        acknowledged_at DATETIME, content_hash TEXT
    )
"""


def _folio(slug, **kw):
    base = dict(folio_id=slug, type="issue", site_id="site-a",
                created_at=datetime(2026, 1, 1, tzinfo=UTC), created_by="agent-x",
                title="A title", content="body", status="open", metadata={})
    base.update(kw)
    return Folio(**base)


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _add_legacy_refs_control(path: Path) -> None:
    """Restore the pre-contraction refs control columns on a current-DDL db (see
    the module docstring's fixture note). Defaults mirror the old DDL."""
    c = _conn(path)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(refs)")}
        if "status" not in cols:
            c.execute("ALTER TABLE refs ADD COLUMN status TEXT DEFAULT 'open'")
            c.execute("ALTER TABLE refs ADD COLUMN assigned_to TEXT")
            c.execute("ALTER TABLE refs ADD COLUMN archived INTEGER DEFAULT 0")
        c.commit()
    finally:
        c.close()


def _build(path: Path, slugs=("folio-1", "folio-2")) -> Path:
    """A faithful pre-A5 db: versions/refs/threads via the real storage layer
    (plus the legacy refs control columns the contraction dropped), plus a legacy
    folios table mirroring refs (the dual-write shape the drop operates on)."""
    db = LogDatabase(path)
    for s in slugs:
        db.save_folio(_folio(s))
    _add_legacy_refs_control(path)
    c = _conn(path)
    try:
        c.execute(_LEGACY_FOLIOS_DDL)
        for r in c.execute("SELECT * FROM refs").fetchall():
            v = c.execute("SELECT * FROM versions WHERE content_hash=?",
                          (r["head_hash"],)).fetchone()
            c.execute(
                "INSERT OR REPLACE INTO folios (folio_id, type, site_id, created_at, "
                "created_by, title, content, status, assigned_to, target_agent, omlet, "
                "archived, metadata, acknowledged_at, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["slug"], v["type"], r["site_id"], v["created_at"], v["created_by"],
                 v["title"], v["content"], r["status"], r["assigned_to"],
                 r["target_agent"], r["omlet"], r["archived"], r["metadata"],
                 r["acknowledged_at"], r["head_hash"]))
        c.commit()
    finally:
        c.close()
    return path


def _precheck(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return df.check_preconditions(c)
    finally:
        c.close()


# ── precondition: the happy path ──────────────────────────────────────────────

def test_precondition_passes_on_clean_mirrored_db(tmp_path):
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    try:
        f = c.execute("SELECT * FROM folios WHERE folio_id='folio-1'").fetchone()
        r = c.execute("SELECT * FROM refs WHERE slug='folio-1'").fetchone()
        assert compute_folio_hash({
            "type": f["type"],
            "title": f["title"],
            "content": f["content"],
            "created_at": f["created_at"],
            "created_by": f["created_by"],
        }) == r["head_hash"]
    finally:
        c.close()

    ok, violations = _precheck(path)
    assert ok and violations == []


def test_precondition_no_folios_table_is_noop_pass(tmp_path):
    # A db already dropped / born folios-free: no folios table -> pass, nothing to do.
    LogDatabase(tmp_path / "skein.db")  # versions/refs/threads, no folios
    ok, violations = _precheck(tmp_path / "skein.db")
    assert ok and violations == []


def test_precondition_refuses_missing_refs(tmp_path):
    # folios present but no refs/versions (pre-Phase-2) -> refuse, don't drop.
    path = tmp_path / "skein.db"
    c = sqlite3.connect(path)
    c.execute(_LEGACY_FOLIOS_DDL)
    c.execute("INSERT INTO folios (folio_id, type, site_id, created_at, created_by, "
              "title, content) VALUES ('f','issue','s','2026-01-01','a','t','b')")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("refs/versions" in v for v in violations)


# ── precondition: each unsafe shape is refused ────────────────────────────────

def test_precondition_folios_not_in_refs(tmp_path):
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("INSERT INTO folios (folio_id, type, site_id, created_at, created_by, "
              "title, content, status) VALUES "
              "('orphan-1','issue','site-a','2026-01-01','a','t','b','open')")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("not in refs" in v for v in violations)


def test_precondition_nonopen_status_without_thread(tmp_path):
    # folios.status non-open with NO covering status thread -> the non-open status
    # would be lost by the drop. Blocked.
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE folios SET status='closed' WHERE folio_id='folio-1'")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("no covering status thread" in v for v in violations)


def test_precondition_nonopen_status_with_thread_passes(tmp_path):
    # Same non-open status, but a covering genesis-keyed status thread exists -> safe.
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    gen = c.execute("SELECT genesis_hash FROM refs WHERE slug='folio-1'").fetchone()[0]
    c.execute("UPDATE folios SET status='closed' WHERE folio_id='folio-1'")
    c.execute("INSERT INTO threads (thread_id, from_id, to_id, type, content, weaver, "
              "created_at) VALUES ('t-close', ?, ?, 'status', 'closed', 'agent-x', "
              "'2026-01-02T00:00:00+00:00')", (gen, gen))
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert ok, violations


def test_precondition_nonopen_status_with_only_slug_keyed_thread_blocks(tmp_path):
    # A4's slug-keyed status threads are retired. The live reader is genesis-keyed,
    # so a non-open folios.status covered only by a slug thread would be lost.
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE folios SET status='closed' WHERE folio_id='folio-1'")
    c.execute("INSERT INTO threads (thread_id, from_id, to_id, type, content, weaver, "
              "created_at) VALUES ('t-close-slug', 'folio-1', 'folio-1', 'status', "
              "'closed', 'agent-x', '2026-01-02T00:00:00+00:00')")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("no covering status thread" in v for v in violations)


def test_precondition_assigned_to_set(tmp_path):
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE folios SET assigned_to='bob' WHERE folio_id='folio-1'")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("assigned_to" in v for v in violations)


def test_precondition_archived_set(tmp_path):
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE folios SET archived=1 WHERE folio_id='folio-1'")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("archived" in v for v in violations)


def test_precondition_mirror_mismatch(tmp_path):
    # folios.target_agent diverges from refs.target_agent -> unmirrored control.
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE folios SET target_agent='ghost' WHERE folio_id='folio-1'")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("mirror mismatch" in v for v in violations)


def test_precondition_identity_hash_mismatch(tmp_path):
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE folios SET title='A diverged title' WHERE folio_id='folio-1'")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("identity does not hash to the head" in v
                          for v in violations)


def test_precondition_head_version_self_verify_mismatch(tmp_path):
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE versions SET title='A corrupt head row' WHERE content_hash = "
              "(SELECT head_hash FROM refs WHERE slug='folio-1')")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("head version does not self-verify" in v
                          for v in violations)


def test_precondition_site_id_mismatch(tmp_path):
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE folios SET site_id='site-b' WHERE folio_id='folio-1'")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("site_id != refs.site_id" in v for v in violations)


def test_precondition_content_hash_mismatch(tmp_path):
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE folios SET content_hash='sha256::deadbeef' "
              "WHERE folio_id='folio-1'")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert not ok and any("content_hash != refs.head_hash" in v
                          for v in violations)


def test_precondition_metadata_null_vs_empty_object_is_not_a_mismatch(tmp_path):
    # NULL folios.metadata vs '{}' refs.metadata both read as {} -> not a mismatch.
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE folios SET metadata=NULL WHERE folio_id='folio-1'")
    c.execute("UPDATE refs SET metadata='{}' WHERE slug='folio-1'")
    c.commit()
    c.close()
    ok, violations = _precheck(path)
    assert ok, violations


# ── drop_one: dry-run vs live ─────────────────────────────────────────────────

def test_dry_run_leaves_live_untouched(tmp_path):
    path = _build(tmp_path / "skein.db")
    passed, label, problems = df.drop_one("p", path, live=False, stamp="TESTSTAMP")
    assert passed, (label, problems)
    # the LIVE db still has its folios table (dry-run worked on a copy)
    c = _conn(path)
    try:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                         "name='folios'").fetchone() is not None
    finally:
        c.close()
    assert not list(path.parent.glob(f"{path.name}.bak-folios-drop-*"))


def test_live_backs_up_and_drops(tmp_path):
    path = _build(tmp_path / "skein.db")
    before = {}
    c = _conn(path)
    for t in ("versions", "refs", "threads"):
        before[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    c.close()

    passed, label, problems = df.drop_one("p", path, live=True, stamp="STAMP1")
    assert passed, (label, problems)

    c = _conn(path)
    try:
        # folios gone; versions/refs/threads unchanged
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='folios'").fetchone() is None
        for t, n in before.items():
            assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n
    finally:
        c.close()
    # the backup exists and still HAS folios (the restore point predates the drop)
    baks = list(path.parent.glob(f"{path.name}.bak-folios-drop-STAMP1"))
    assert len(baks) == 1
    bc = _conn(baks[0])
    try:
        assert bc.execute("SELECT 1 FROM sqlite_master WHERE name='folios'").fetchone() is not None
    finally:
        bc.close()


def test_live_refuses_when_prior_backup_exists(tmp_path):
    path = _build(tmp_path / "skein.db")
    # a prior backup from an earlier attempt
    (path.parent / f"{path.name}.bak-folios-drop-OLDSTAMP").write_text("x")
    passed, label, problems = df.drop_one("p", path, live=True, stamp="NEW")
    assert not passed and "prior folios-drop backup" in label
    # live db untouched — folios still present
    c = _conn(path)
    try:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='folios'").fetchone() is not None
    finally:
        c.close()


def test_live_refuses_exact_backup_destination_race(tmp_path, monkeypatch):
    path = _build(tmp_path / "skein.db")
    stamp = "RACESTAMP"
    pre = path.parent / f"{path.name}.bak-folios-drop-{stamp}"
    pre.write_text("original backup bytes")

    # Simulate a direct-driver race where the prior-backup glob saw nothing, but
    # the exact stamped destination exists before the restore-point copy opens it.
    monkeypatch.setattr(type(path.parent), "glob", lambda self, pattern: iter(()))

    passed, label, problems = df.drop_one("p", path, live=True, stamp=stamp)
    assert not passed and "backup destination already exists" in label
    assert problems == ["backup destination already exists"]
    assert pre.read_text() == "original backup bytes"

    c = _conn(path)
    try:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='folios'").fetchone() is not None
    finally:
        c.close()


def test_live_backup_restore_point_has_folios_before_drop(tmp_path, monkeypatch):
    path = _build(tmp_path / "skein.db")

    real_backup = df._backup_copy

    def backup_without_folios(src, dst):
        real_backup(src, dst)
        c = sqlite3.connect(dst)
        try:
            c.execute("DROP TABLE folios")
            c.commit()
        finally:
            c.close()

    monkeypatch.setattr(df, "_backup_copy", backup_without_folios)

    passed, label, problems = df.drop_one("p", path, live=True, stamp="BADBAK")
    assert not passed and "backup missing folios" in label
    assert problems == ["backup missing folios"]

    c = _conn(path)
    try:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='folios'").fetchone() is not None
    finally:
        c.close()


def test_live_blocked_precondition_does_not_drop(tmp_path):
    path = _build(tmp_path / "skein.db")
    c = _conn(path)
    c.execute("UPDATE folios SET assigned_to='bob' WHERE folio_id='folio-1'")
    c.commit()
    c.close()
    passed, label, problems = df.drop_one("p", path, live=True, stamp="STAMP")
    assert not passed and "precondition REFUSED" in label
    # folios still present, no backup written (fail-closed BEFORE any mutation)
    c = _conn(path)
    try:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='folios'").fetchone() is not None
    finally:
        c.close()
    assert not list(path.parent.glob(f"{path.name}.bak-folios-drop-*"))


def test_no_folios_table_is_noop_pass(tmp_path):
    # An already-folios-free db: drop_one passes as a no-op and writes no backup.
    path = tmp_path / "skein.db"
    LogDatabase(path)
    passed, label, problems = df.drop_one("p", path, live=True, stamp="STAMP")
    assert passed and "no-op" in label
    assert not list(path.parent.glob(f"{path.name}.bak-folios-drop-*"))


def test_verify_dropped_flags_collateral_row_loss(tmp_path):
    # verify_dropped must catch a "drop" that also removed rows from another table.
    _build(tmp_path / "skein.db")
    c = _conn(tmp_path / "skein.db")
    before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("versions", "refs", "threads")}
    c.execute("DROP TABLE folios")
    c.execute("DELETE FROM refs")  # collateral damage
    c.commit()
    ok, problems = df.verify_dropped(c, before)
    c.close()
    assert not ok and any("refs row count changed" in p for p in problems)
