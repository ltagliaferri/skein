"""Conformance gate for Phase 3 step 0: retiring the legacy folios_fts index.

Pins two halves of the cut:
  - The CODE change: a freshly initialized db no longer creates folios_fts or its
    three sync triggers (storage._init_db), while versions_fts (the live read
    index) is still present.
  - The MIGRATION: retire_folios_fts.retire_db drops folios_fts + its triggers
    from a legacy-shaped db, leaves the folios CONTENT untouched, is idempotent,
    and dry-run never mutates.
"""

import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skein.migrations.retire_folios_fts import retire_db, _present, _backup_db
from skein.models import Folio, Site
from skein.storage import JSONStore


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d)


@pytest.fixture
def store(tmp_dir):
    return JSONStore(tmp_dir)


def _db(store):
    return store.base_dir / "skein.db"


def _conn(store):
    c = sqlite3.connect(_db(store))
    c.row_factory = sqlite3.Row
    return c


def _seed_folio(store, folio_id="finding-20260630-aaaa"):
    store.save_site(Site(
        site_id="alpha", created_at=datetime.now(timezone.utc),
        created_by="t", purpose="p",
    ))
    store.save_folio(Folio(
        folio_id=folio_id, type="finding", site_id="alpha",
        created_at=datetime.now(timezone.utc), created_by="a",
        title="T", content="genesis body", status="open", metadata={},
    ))


# The legacy DDL this migration retires — recreated here so the test owns a
# faithful pre-migration db without depending on old code.
_LEGACY_FTS_DDL = [
    """CREATE VIRTUAL TABLE IF NOT EXISTS folios_fts USING fts5(
        folio_id, title, content, content=folios, content_rowid=rowid)""",
    """CREATE TRIGGER IF NOT EXISTS folios_ai AFTER INSERT ON folios BEGIN
        INSERT INTO folios_fts(rowid, folio_id, title, content)
        VALUES (new.rowid, new.folio_id, new.title, new.content);
    END""",
    """CREATE TRIGGER IF NOT EXISTS folios_ad AFTER DELETE ON folios BEGIN
        INSERT INTO folios_fts(folios_fts, rowid, folio_id, title, content)
        VALUES('delete', old.rowid, old.folio_id, old.title, old.content);
    END""",
    """CREATE TRIGGER IF NOT EXISTS folios_au AFTER UPDATE ON folios BEGIN
        INSERT INTO folios_fts(folios_fts, rowid, folio_id, title, content)
        VALUES('delete', old.rowid, old.folio_id, old.title, old.content);
        INSERT INTO folios_fts(rowid, folio_id, title, content)
        VALUES (new.rowid, new.folio_id, new.title, new.content);
    END""",
]


def _install_legacy_fts(store):
    """Put folios_fts + its triggers back on a (post-change) db, then index the
    existing folios — a faithful legacy-shaped db for the migration to retire."""
    c = _conn(store)
    try:
        for ddl in _LEGACY_FTS_DDL:
            c.execute(ddl)
        c.execute(
            "INSERT INTO folios_fts(rowid, folio_id, title, content) "
            "SELECT rowid, folio_id, title, content FROM folios"
        )
        c.commit()
    finally:
        c.close()


def _present_on(store):
    c = _conn(store)
    try:
        return _present(c)
    finally:
        c.close()


def test_fresh_db_has_no_folios_fts(store):
    # The _init_db change: no folios_fts, no folios triggers, versions_fts intact.
    c = _conn(store)
    try:
        fts = c.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'folios_fts%'"
        ).fetchall()
        trg = c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='folios'"
        ).fetchall()
        vfts = c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='versions_fts'"
        ).fetchone()
    finally:
        c.close()
    assert fts == []
    assert trg == []
    assert vfts is not None


def test_retire_drops_table_and_triggers(store):
    _seed_folio(store)
    _install_legacy_fts(store)
    before = _present_on(store)
    assert before["table"] is True
    assert before["triggers"] == ["folios_ad", "folios_ai", "folios_au"]

    folios_before = _conn(store).execute("SELECT COUNT(*) FROM folios").fetchone()[0]
    stats = retire_db(_db(store), dry_run=False)

    assert stats["dropped_table"] is True
    assert stats["dropped_triggers"] == ["folios_ad", "folios_ai", "folios_au"]
    after = _present_on(store)
    assert after["table"] is False
    assert after["triggers"] == []
    # CONTENT table untouched, and no folios_fts shadow tables left behind.
    c = _conn(store)
    try:
        assert c.execute("SELECT COUNT(*) FROM folios").fetchone()[0] == folios_before
        assert c.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'folios_fts%'"
        ).fetchall() == []
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()


def test_retire_is_idempotent(store):
    _seed_folio(store)
    _install_legacy_fts(store)
    retire_db(_db(store), dry_run=False)
    # Second run: nothing present, clean no-op.
    stats = retire_db(_db(store), dry_run=False)
    assert stats["table_before"] is False
    assert stats["triggers_before"] == []
    assert stats["dropped_table"] is False
    assert stats["dropped_triggers"] == []


def test_dry_run_does_not_mutate(store):
    _seed_folio(store)
    _install_legacy_fts(store)
    stats = retire_db(_db(store), dry_run=True)
    assert stats["table_before"] is True
    assert stats["triggers_before"] == ["folios_ad", "folios_ai", "folios_au"]
    # Still present after a dry run.
    after = _present_on(store)
    assert after["table"] is True
    assert after["triggers"] == ["folios_ad", "folios_ai", "folios_au"]


def test_backup_snapshots_the_pre_retire_db(store):
    # --backup must capture a consistent snapshot BEFORE the drop: the backup
    # still holds folios_fts and the same folios count; the live db is retired.
    _seed_folio(store)
    _install_legacy_fts(store)
    folios_n = _conn(store).execute("SELECT COUNT(*) FROM folios").fetchone()[0]

    backup = _backup_db(_db(store))
    retire_db(_db(store), dry_run=False)

    assert backup.exists() and backup != _db(store)
    bc = sqlite3.connect(backup)
    try:
        assert bc.execute(
            "SELECT 1 FROM sqlite_master WHERE name='folios_fts'"
        ).fetchone() is not None  # backup pre-dates the drop
        assert bc.execute("SELECT COUNT(*) FROM folios").fetchone()[0] == folios_n
    finally:
        bc.close()
    # Live db is retired; backup is the rollback.
    assert _present_on(store)["table"] is False


def test_writes_still_work_after_retire(store):
    # With the triggers gone, INSERT OR REPLACE INTO folios must still succeed.
    _seed_folio(store)
    _install_legacy_fts(store)
    retire_db(_db(store), dry_run=False)
    # An edit through the live path (save_folio does INSERT OR REPLACE).
    f = store.get_folio("finding-20260630-aaaa")
    f.content = "edited body"
    assert store.save_folio(f, editor="e") is not False
    assert store.get_folio("finding-20260630-aaaa").content == "edited body"
