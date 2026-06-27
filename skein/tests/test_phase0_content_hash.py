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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skein import identity
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
