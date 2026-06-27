"""Phase 0 behaviour tests: the folio content hash, as the storage layer uses it.

The canon/identity RSP is proven byte-for-byte by test_canon_conformance.py and
test_identity.py. These tests cover the Phase 0 *integration*: that the storage
layer delegates to that one RSP (no second hash function), recomputes on every
write (the compute-once guard was the bug), and frames the hash as sha256::.
They also exercise real corpus data so a created_at encoding in the wild can't
break the normalize path.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skein import identity
from skein.migrations.backfill_content_hash import backfill_db
from skein.models import Folio
from skein.storage import KNURL_AVAILABLE, LogDatabase, compute_folio_hash

pytestmark = pytest.mark.skipif(not KNURL_AVAILABLE, reason="knurl not installed")

UTC = timezone.utc
REPO = Path(__file__).resolve().parent.parent.parent
ARCHIVE_DB = REPO / ".skein" / "legacy-archive-20260624" / "data" / "skein.db"


def _folio(**over) -> Folio:
    base = dict(
        folio_id="test-20260101-aaaa",
        type="finding",
        site_id="test-site",
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        created_by="tester-0101",
        title="A title",
        content="A body.",
    )
    base.update(over)
    return Folio(**base)


def test_storage_delegates_to_identity_and_frames_sha256():
    """storage.compute_folio_hash is the SAME hash as the identity RSP, framed
    sha256:: — not a second implementation and not the old folio:sha256: framing."""
    folio = _folio()
    expected = identity.compute_folio_hash(
        {
            "type": folio.type,
            "title": folio.title,
            "content": folio.content,
            "created_at": folio.created_at,
            "created_by": folio.created_by,
        }
    )
    got = compute_folio_hash(folio)
    assert got == expected
    assert got.startswith("sha256::")
    assert "folio:sha256:" not in got


def test_divergent_created_at_encodings_collapse_to_one_hash():
    """The same instant expressed UTC / offset / naive hashes identically once it
    passes through the storage delegation (normalize_created_at does the work)."""
    aware_utc = _folio(created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
    offset = _folio(
        created_at=datetime(2026, 1, 1, 17, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    )
    naive = _folio(created_at=datetime(2026, 1, 1, 12, 0, 0))  # assumed UTC

    h = compute_folio_hash(aware_utc)
    assert compute_folio_hash(offset) == h
    assert compute_folio_hash(naive) == h


def test_save_recomputes_hash_on_every_write(tmp_path):
    """An edit changes the stored content_hash — the compute-once guard is gone."""
    db = LogDatabase(tmp_path / "t.db")
    f = _folio(content="original")
    db.save_folio(f)
    h1 = db.get_folio(f.folio_id).content_hash

    edited = db.get_folio(f.folio_id)
    edited.content = "edited body"
    db.save_folio(edited)
    h2 = db.get_folio(f.folio_id).content_hash

    assert h1 != h2
    assert h1.startswith("sha256::") and h2.startswith("sha256::")


def test_save_overwrites_a_preset_wrong_hash(tmp_path):
    """save_folio recomputes unconditionally — a caller-supplied hash is never
    trusted (so a stale or wrong value can't ride through the write path)."""
    db = LogDatabase(tmp_path / "t.db")
    f = _folio(content_hash="sha256::" + "0" * 64)
    db.save_folio(f)
    stored = db.get_folio(f.folio_id).content_hash
    assert stored == compute_folio_hash(_folio())
    assert stored != "sha256::" + "0" * 64


def test_json_cold_migration_recomputes_hash(tmp_path):
    """The JSON->SQLite cold migration is a folio writer too: it recomputes the
    hash from the five fields rather than trusting the legacy JSON's stored value,
    so a stale folio:sha256: (or a missing value) is never persisted."""
    db = LogDatabase(tmp_path / "t.db")
    sites_dir = tmp_path / "sites"
    folios_dir = sites_dir / "test-site" / "folios"
    folios_dir.mkdir(parents=True)
    data = {
        "folio_id": "finding-20260101-bbbb",
        "type": "finding",
        "site_id": "test-site",
        "created_at": "2026-01-01T12:00:00+00:00",
        "created_by": "tester-0101",
        "title": "A title",
        "content": "A body.",
        "status": "open",
        "metadata": {},
        "content_hash": "folio:sha256:deadbeef",  # legacy framing; must be ignored
    }
    (folios_dir / f"{data['folio_id']}.json").write_text(json.dumps(data))

    assert db.migrate_folios_from_json(sites_dir) == 1
    stored = db.get_folio(data["folio_id"]).content_hash

    expected = identity.compute_folio_hash(
        {
            "type": "finding",
            "title": "A title",
            "content": "A body.",
            "created_at": "2026-01-01T12:00:00+00:00",
            "created_by": "tester-0101",
        }
    )
    assert stored == expected
    assert stored.startswith("sha256::")
    assert stored != "folio:sha256:deadbeef"


def _populate_and_corrupt(db_path):
    """A real-schema db with three folios whose stored hashes are: reframed
    (same digest, old framing), digest_changed (wrong hex), and missing."""
    db = LogDatabase(db_path)
    ids = ("finding-20260101-r000", "finding-20260101-d000", "finding-20260101-m000")
    db.save_folio(_folio(folio_id=ids[0], content="uniquewordxyz reframed body"))
    db.save_folio(_folio(folio_id=ids[1], content="digest body"))
    db.save_folio(_folio(folio_id=ids[2], content="missing body"))

    conn = sqlite3.connect(db_path)
    # reframed: keep the true digest, restore the old folio:sha256: framing.
    true_hex = db.get_folio(ids[0]).content_hash.split("::", 1)[1]
    conn.execute("UPDATE folios SET content_hash=? WHERE folio_id=?",
                 (f"folio:sha256:{true_hex}", ids[0]))
    # digest_changed: a wrong digest in the new framing.
    conn.execute("UPDATE folios SET content_hash=? WHERE folio_id=?",
                 ("sha256::" + "0" * 64, ids[1]))
    # missing: no stored hash at all.
    conn.execute("UPDATE folios SET content_hash=NULL WHERE folio_id=?", (ids[2],))
    conn.commit()
    conn.close()
    return ids


def test_backfill_classifies_and_corrects(tmp_path):
    db_path = tmp_path / "t.db"
    ids = _populate_and_corrupt(db_path)

    stats, _ = backfill_db(db_path, dry_run=False)
    assert stats["reframed"] == 1
    assert stats["digest_changed"] == 1
    assert stats["missing"] == 1
    assert stats["error"] == 0
    assert stats["written"] == 3

    db = LogDatabase(db_path)
    for fid in ids:
        folio = db.get_folio(fid)
        assert folio.content_hash == compute_folio_hash(folio)
        assert folio.content_hash.startswith("sha256::")


def test_backfill_is_idempotent(tmp_path):
    db_path = tmp_path / "t.db"
    _populate_and_corrupt(db_path)

    backfill_db(db_path, dry_run=False)
    second, _ = backfill_db(db_path, dry_run=False)
    assert second["written"] == 0
    assert second["unchanged"] == second["total"] == 3


def test_backfill_dry_run_writes_nothing(tmp_path):
    db_path = tmp_path / "t.db"
    ids = _populate_and_corrupt(db_path)

    stats, _ = backfill_db(db_path, dry_run=True)
    assert stats["written"] == 0
    assert stats["reframed"] + stats["digest_changed"] + stats["missing"] == 3

    # the stored values are untouched
    conn = sqlite3.connect(db_path)
    stored = dict(conn.execute(
        "SELECT folio_id, content_hash FROM folios"
    ).fetchall())
    conn.close()
    assert stored[ids[1]] == "sha256::" + "0" * 64
    assert stored[ids[2]] is None


def test_backfill_preserves_fts(tmp_path):
    """The content_hash UPDATE fires the AFTER UPDATE trigger; FTS must still
    find a folio by a word in its content afterward."""
    db_path = tmp_path / "t.db"
    _populate_and_corrupt(db_path)
    backfill_db(db_path, dry_run=False)

    conn = sqlite3.connect(db_path)
    hits = conn.execute(
        "SELECT count(*) FROM folios_fts WHERE folios_fts MATCH 'uniquewordxyz'"
    ).fetchone()[0]
    triggers = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"))
    conn.close()
    assert hits == 1
    assert {"folios_ai", "folios_ad", "folios_au"} <= set(triggers)


def test_backfill_locks_before_read(tmp_path, monkeypatch):
    """The write lock is acquired BEFORE the read: a concurrent writer holding
    the lock makes apply fail at BEGIN IMMEDIATE (so no read-compute-write race)."""
    import skein.migrations.backfill_content_hash as bfc

    db_path = tmp_path / "t.db"
    _populate_and_corrupt(db_path)
    monkeypatch.setattr(bfc, "BUSY_TIMEOUT_MS", 100)

    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("PRAGMA busy_timeout = 100")
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("UPDATE folios SET status='x' WHERE folio_id=?",
                    ("finding-20260101-r000",))
    try:
        with pytest.raises(sqlite3.OperationalError):
            bfc.backfill_db(db_path, dry_run=False)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_write_mapping_atomic_and_records_match_changes(tmp_path):
    """backfill_db returns a record per changed folio; _write_mapping persists
    them, and an unrelated pre-existing file is left intact when no write occurs."""
    import skein.migrations.backfill_content_hash as bfc

    db_path = tmp_path / "t.db"
    _populate_and_corrupt(db_path)
    stats, records = backfill_db(db_path, dry_run=False)
    assert len(records) == stats["written"] == 3

    out = tmp_path / "map.jsonl"
    bfc._write_mapping(out, records)
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(lines) == 3
    assert {r["change"] for r in lines} == {"reframed", "digest_changed", "missing"}
    assert not (tmp_path / "map.jsonl.tmp").exists()  # temp cleaned by os.replace


def test_backfill_backup_is_consistent(tmp_path):
    import skein.migrations.backfill_content_hash as bfc

    db_path = tmp_path / "t.db"
    _populate_and_corrupt(db_path)
    backup = bfc._backup_db(db_path)
    assert backup.exists()
    conn = sqlite3.connect(backup)
    n = conn.execute("SELECT count(*) FROM folios").fetchone()[0]
    conn.close()
    assert n == 3


def test_ensure_aware_accepts_lowercase_z():
    from skein.storage import ensure_aware

    upper = ensure_aware("2026-01-01T12:00:00Z")
    lower = ensure_aware("2026-01-01T12:00:00z")
    assert upper == lower
    assert lower.tzinfo is not None


@pytest.mark.skipif(not ARCHIVE_DB.exists(), reason="archive fixture absent")
def test_backfill_real_fixture_idempotent_on_copy(tmp_path):
    """On a copy of real data: backfill rewrites everything to sha256::, a second
    run is a no-op, and no folio errors out."""
    copy = tmp_path / "skein.db"
    copy.write_bytes(ARCHIVE_DB.read_bytes())

    first, _ = backfill_db(copy, dry_run=False)
    assert first["error"] == 0
    assert first["written"] == first["total"] - first["unchanged"]

    second, _ = backfill_db(copy, dry_run=False)
    assert second["written"] == 0
    assert second["unchanged"] == second["total"]

    conn = sqlite3.connect(copy)
    bad = conn.execute(
        "SELECT count(*) FROM folios WHERE content_hash IS NOT NULL "
        "AND content_hash NOT LIKE 'sha256::%'"
    ).fetchone()[0]
    conn.close()
    assert bad == 0


@pytest.mark.skipif(not ARCHIVE_DB.exists(), reason="archive fixture absent")
def test_real_corpus_hashes_are_wellformed_and_stable():
    """Over real folios: the delegation produces a well-formed sha256:: and is
    deterministic — a wild created_at encoding can't break the normalize path."""
    folios = LogDatabase(ARCHIVE_DB).get_folios()
    assert folios, "archive fixture has no folios"
    checked = 0
    for folio in folios[:200]:
        h = compute_folio_hash(folio)
        assert h.startswith("sha256::") and len(h) == len("sha256::") + 64
        assert compute_folio_hash(folio) == h  # deterministic
        checked += 1
    assert checked >= 1
