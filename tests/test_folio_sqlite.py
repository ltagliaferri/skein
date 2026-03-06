"""Tests for SQLite-backed folio storage."""

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skein.storage import LogDatabase, JSONStore
from skein.models import Folio, Site


@pytest.fixture
def tmp_dir():
    """Create a temporary directory for test data."""
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d)


@pytest.fixture
def db(tmp_dir):
    """Create a LogDatabase in temp dir."""
    return LogDatabase(tmp_dir / "test.db")


@pytest.fixture
def store(tmp_dir):
    """Create a JSONStore in temp dir (no auto-migration since sites/ is empty)."""
    return JSONStore(tmp_dir)


def make_folio(folio_id="test-20260303-abcd", site_id="test-site", **kwargs):
    """Helper to create a Folio with defaults."""
    defaults = dict(
        folio_id=folio_id,
        type="issue",
        site_id=site_id,
        created_at=datetime.now(timezone.utc),
        created_by="test-agent",
        title="Test issue title for testing",
        content="This is test content for the issue",
        status="open",
        metadata={},
    )
    defaults.update(kwargs)
    return Folio(**defaults)


# --- LogDatabase folio tests ---


class TestLogDatabaseFolios:
    def test_save_and_get_folio(self, db):
        folio = make_folio()
        assert db.save_folio(folio)

        retrieved = db.get_folio("test-20260303-abcd")
        assert retrieved is not None
        assert retrieved.folio_id == "test-20260303-abcd"
        assert retrieved.title == "Test issue title for testing"
        assert retrieved.type == "issue"
        assert retrieved.site_id == "test-site"

    def test_get_folio_not_found(self, db):
        assert db.get_folio("nonexistent") is None

    def test_save_folio_upsert(self, db):
        folio = make_folio(title="Original title here")
        db.save_folio(folio)

        folio.title = "Updated title here please"
        db.save_folio(folio)

        retrieved = db.get_folio("test-20260303-abcd")
        assert retrieved.title == "Updated title here please"

    def test_get_folios_all(self, db):
        db.save_folio(make_folio("folio-1", "site-a"))
        db.save_folio(make_folio("folio-2", "site-b"))
        db.save_folio(make_folio("folio-3", "site-a"))

        all_folios = db.get_folios()
        assert len(all_folios) == 3

    def test_get_folios_by_site(self, db):
        db.save_folio(make_folio("folio-1", "site-a"))
        db.save_folio(make_folio("folio-2", "site-b"))
        db.save_folio(make_folio("folio-3", "site-a"))

        site_a = db.get_folios(site_id="site-a")
        assert len(site_a) == 2

        site_b = db.get_folios(site_id="site-b")
        assert len(site_b) == 1

    def test_get_folios_by_type(self, db):
        db.save_folio(make_folio("folio-1", type="issue"))
        db.save_folio(make_folio("folio-2", type="brief"))
        db.save_folio(make_folio("folio-3", type="issue"))

        issues = db.get_folios(type="issue")
        assert len(issues) == 2

    def test_get_folios_by_status(self, db):
        db.save_folio(make_folio("folio-1", status="open"))
        db.save_folio(make_folio("folio-2", status="closed"))

        open_folios = db.get_folios(status="open")
        assert len(open_folios) == 1

    def test_get_folios_by_archived(self, db):
        db.save_folio(make_folio("folio-1", archived=False))
        db.save_folio(make_folio("folio-2", archived=True))

        active = db.get_folios(archived=False)
        assert len(active) == 1
        assert active[0].folio_id == "folio-1"

    def test_get_folios_by_created_by(self, db):
        db.save_folio(make_folio("folio-1", created_by="agent-a"))
        db.save_folio(make_folio("folio-2", created_by="agent-b"))

        by_a = db.get_folios(created_by="agent-a")
        assert len(by_a) == 1

    def test_move_folio(self, db):
        db.save_folio(make_folio("folio-1", "site-a"))

        moved = db.move_folio("folio-1", "site-b")
        assert moved is not None
        assert moved.site_id == "site-b"

        # Verify in DB
        retrieved = db.get_folio("folio-1")
        assert retrieved.site_id == "site-b"

    def test_move_folio_not_found(self, db):
        assert db.move_folio("nonexistent", "site-b") is None

    def test_search_folios(self, db):
        db.save_folio(
            make_folio(
                "folio-1",
                title="Database migration issue",
                content="Move folios to SQLite",
            )
        )
        db.save_folio(
            make_folio(
                "folio-2", title="UI bug in dashboard", content="Button doesn't work"
            )
        )
        db.save_folio(
            make_folio(
                "folio-3",
                title="Performance optimization",
                content="Speed up database queries",
            )
        )

        results = db.search_folios("database")
        assert len(results) >= 1
        folio_ids = {f.folio_id for f in results}
        assert "folio-1" in folio_ids

    def test_search_folios_no_results(self, db):
        db.save_folio(make_folio("folio-1", title="Some random title here"))
        results = db.search_folios("zzzznonexistent")
        assert len(results) == 0

    def test_folio_count(self, db):
        db.save_folio(make_folio("folio-1", "site-a"))
        db.save_folio(make_folio("folio-2", "site-b"))
        db.save_folio(make_folio("folio-3", "site-a"))

        assert db.get_folio_count() == 3
        assert db.get_folio_count(site_id="site-a") == 2
        assert db.get_folio_count(site_id="site-b") == 1

    def test_folio_stats(self, db):
        db.save_folio(make_folio("folio-1", type="issue", status="open"))
        db.save_folio(make_folio("folio-2", type="brief", status="open"))
        db.save_folio(make_folio("folio-3", type="issue", status="closed"))

        stats = db.get_folio_stats()
        assert stats["total"] == 3
        assert stats["by_type"]["issue"] == 2
        assert stats["by_type"]["brief"] == 1
        assert stats["by_status"]["open"] == 2
        assert stats["by_status"]["closed"] == 1

    def test_folio_metadata(self, db):
        folio = make_folio(metadata={"key": "value", "nested": {"a": 1}})
        db.save_folio(folio)

        retrieved = db.get_folio("test-20260303-abcd")
        assert retrieved.metadata["key"] == "value"
        assert retrieved.metadata["nested"]["a"] == 1

    def test_folio_optional_fields(self, db):
        folio = make_folio(
            assigned_to="agent-x",
            target_agent="agent-y",
            omlet="strand-123/agent-x/turn-5",
            acknowledged_at=datetime.now(timezone.utc),
            content_hash="folio-abc123",
        )
        db.save_folio(folio)

        retrieved = db.get_folio("test-20260303-abcd")
        assert retrieved.assigned_to == "agent-x"
        assert retrieved.target_agent == "agent-y"
        assert retrieved.omlet == "strand-123/agent-x/turn-5"
        assert retrieved.acknowledged_at is not None
        assert retrieved.content_hash == "folio-abc123"


# --- Migration tests ---


class TestFolioMigration:
    def _make_json_folio(self, sites_dir, folio_id, site_id="test-site", **kwargs):
        """Create a JSON folio file on disk."""
        folios_dir = sites_dir / site_id / "folios"
        folios_dir.mkdir(parents=True, exist_ok=True)

        data = {
            "folio_id": folio_id,
            "type": kwargs.get("type", "issue"),
            "site_id": site_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": "test-agent",
            "title": kwargs.get("title", f"Title for {folio_id}"),
            "content": kwargs.get("content", f"Content for {folio_id}"),
            "status": "open",
            "assigned_to": None,
            "target_agent": None,
            "archived": False,
            "metadata": {},
            "acknowledged_at": None,
        }
        data.update({k: v for k, v in kwargs.items() if k in data})

        with open(folios_dir / f"{folio_id}.json", "w") as f:
            json.dump(data, f)

    def test_migrate_basic(self, tmp_dir):
        db = LogDatabase(tmp_dir / "test.db")
        sites_dir = tmp_dir / "sites"

        self._make_json_folio(sites_dir, "issue-20260303-0001")
        self._make_json_folio(sites_dir, "brief-20260303-0002")

        count = db.migrate_folios_from_json(sites_dir)
        assert count == 2

        all_folios = db.get_folios()
        assert len(all_folios) == 2

    def test_migrate_idempotent(self, tmp_dir):
        db = LogDatabase(tmp_dir / "test.db")
        sites_dir = tmp_dir / "sites"

        self._make_json_folio(sites_dir, "issue-20260303-0001")

        count1 = db.migrate_folios_from_json(sites_dir)
        assert count1 == 1

        # Recreate the folios dir (simulating it wasn't renamed)
        folios_dir = sites_dir / "test-site" / "folios"
        folios_dir.mkdir(exist_ok=True)
        self._make_json_folio(sites_dir, "issue-20260303-0002")

        count2 = db.migrate_folios_from_json(sites_dir)
        assert count2 == 0  # Already has data, skip

    def test_migrate_creates_backup(self, tmp_dir):
        db = LogDatabase(tmp_dir / "test.db")
        sites_dir = tmp_dir / "sites"

        self._make_json_folio(sites_dir, "issue-20260303-0001")

        db.migrate_folios_from_json(sites_dir)

        # Original folios dir should be renamed
        assert (sites_dir / "test-site" / "folios_migrated").exists()
        assert not (sites_dir / "test-site" / "folios").exists()

    def test_migrate_fts_populated(self, tmp_dir):
        db = LogDatabase(tmp_dir / "test.db")
        sites_dir = tmp_dir / "sites"

        self._make_json_folio(
            sites_dir,
            "issue-20260303-0001",
            title="Database migration issue",
            content="Move folios to SQLite storage",
        )

        db.migrate_folios_from_json(sites_dir)

        results = db.search_folios("database")
        assert len(results) == 1
        assert results[0].folio_id == "issue-20260303-0001"

    def test_migrate_multiple_sites(self, tmp_dir):
        db = LogDatabase(tmp_dir / "test.db")
        sites_dir = tmp_dir / "sites"

        self._make_json_folio(sites_dir, "issue-1", "site-a")
        self._make_json_folio(sites_dir, "issue-2", "site-b")
        self._make_json_folio(sites_dir, "issue-3", "site-a")

        count = db.migrate_folios_from_json(sites_dir)
        assert count == 3

        site_a = db.get_folios(site_id="site-a")
        assert len(site_a) == 2

    def test_migrate_empty_sites_dir(self, tmp_dir):
        db = LogDatabase(tmp_dir / "test.db")
        sites_dir = tmp_dir / "sites"
        sites_dir.mkdir()

        count = db.migrate_folios_from_json(sites_dir)
        assert count == 0

    def test_migrate_nonexistent_dir(self, tmp_dir):
        db = LogDatabase(tmp_dir / "test.db")
        count = db.migrate_folios_from_json(tmp_dir / "nonexistent")
        assert count == 0

    def test_migrate_handles_missing_fields(self, tmp_dir):
        """Test migration with minimal JSON (missing optional fields)."""
        db = LogDatabase(tmp_dir / "test.db")
        sites_dir = tmp_dir / "sites"

        folios_dir = sites_dir / "test-site" / "folios"
        folios_dir.mkdir(parents=True)

        # Minimal folio with only required fields
        data = {
            "folio_id": "issue-20260303-0001",
            "type": "issue",
            "site_id": "test-site",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": "test-agent",
            "title": "Minimal folio for testing",
            "content": "Minimal content here",
        }
        with open(folios_dir / "issue-20260303-0001.json", "w") as f:
            json.dump(data, f)

        count = db.migrate_folios_from_json(sites_dir)
        assert count == 1

        folio = db.get_folio("issue-20260303-0001")
        assert folio.status == "open"
        assert folio.archived is False


# --- JSONStore integration tests ---


class TestJSONStoreFolios:
    def test_save_and_get_via_store(self, store):
        # Create a site first (sites still need JSON dir)
        site = Site(
            site_id="test-site",
            created_at=datetime.now(timezone.utc),
            created_by="test-agent",
            purpose="Testing",
        )
        store.save_site(site)

        folio = make_folio()
        assert store.save_folio(folio)

        retrieved = store.get_folio("test-20260303-abcd")
        assert retrieved is not None
        assert retrieved.title == "Test issue title for testing"

    def test_get_folios_via_store(self, store):
        folio1 = make_folio("folio-1", "site-a")
        folio2 = make_folio("folio-2", "site-b")
        store.save_folio(folio1)
        store.save_folio(folio2)

        all_folios = store.get_folios()
        assert len(all_folios) == 2

        site_a = store.get_folios(site_id="site-a")
        assert len(site_a) == 1

    def test_move_folio_via_store(self, store):
        # Create dest site dir
        (store.sites_dir / "dest-site").mkdir()

        folio = make_folio("folio-1", "site-a")
        store.save_folio(folio)

        moved = store.move_folio("folio-1", "dest-site")
        assert moved.site_id == "dest-site"

    def test_move_folio_dest_not_exists(self, store):
        folio = make_folio("folio-1", "site-a")
        store.save_folio(folio)

        with pytest.raises(ValueError, match="does not exist"):
            store.move_folio("folio-1", "nonexistent-site")

    def test_search_via_store(self, store):
        store.save_folio(
            make_folio(
                "folio-1", title="Database migration plan", content="Move to SQLite"
            )
        )
        store.save_folio(
            make_folio(
                "folio-2", title="UI improvements plan", content="Better buttons"
            )
        )

        results = store.search_folios("database")
        assert len(results) >= 1

    def test_auto_migration_on_init(self, tmp_dir):
        """Test that JSONStore auto-migrates folios from JSON on init."""
        sites_dir = tmp_dir / "sites"
        folios_dir = sites_dir / "test-site" / "folios"
        folios_dir.mkdir(parents=True)

        # Create site metadata
        site_meta_dir = sites_dir / "test-site"
        with open(site_meta_dir / "metadata.json", "w") as f:
            json.dump(
                {
                    "site_id": "test-site",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "created_by": "test",
                    "purpose": "test",
                },
                f,
            )

        # Create a JSON folio
        folio_data = {
            "folio_id": "issue-20260303-auto",
            "type": "issue",
            "site_id": "test-site",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": "test-agent",
            "title": "Auto-migrated folio test",
            "content": "Should be auto-migrated",
            "status": "open",
            "assigned_to": None,
            "target_agent": None,
            "archived": False,
            "metadata": {},
            "acknowledged_at": None,
        }
        with open(folios_dir / "issue-20260303-auto.json", "w") as f:
            json.dump(folio_data, f)

        # Create JSONStore - should auto-migrate
        store = JSONStore(tmp_dir)

        # Folio should be in SQLite now
        folio = store.get_folio("issue-20260303-auto")
        assert folio is not None
        assert folio.title == "Auto-migrated folio test"

        # Original dir should be backed up
        assert (sites_dir / "test-site" / "folios_migrated").exists()
