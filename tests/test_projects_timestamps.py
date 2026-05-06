"""Tests for the GET /projects/timestamps cross-project bulk-activity endpoint."""

import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skein_server import app
from skein import storage as storage_mod
from skein.models import Folio, Site
from skein.storage import JSONStore


@pytest.fixture
def projects_root():
    d = Path(tempfile.mkdtemp(prefix="skein_projects_timestamps_test_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_registry(monkeypatch):
    """Inject a controlled in-memory registry without touching ~/.skein/projects.json."""
    state = {"registry": {}}

    def loader():
        return state["registry"]

    monkeypatch.setattr(storage_mod, "load_project_registry", loader)
    return state


@pytest.fixture
def client():
    return TestClient(app)


def make_project_store(projects_root: Path, project_name: str) -> JSONStore:
    data_dir = projects_root / project_name / ".skein" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return JSONStore(data_dir)


def register_project(fake_registry, projects_root: Path, project_name: str) -> Path:
    data_dir = projects_root / project_name / ".skein" / "data"
    fake_registry["registry"][project_name] = {
        "path": str(projects_root / project_name),
        "data_dir": str(data_dir),
        "name": project_name,
    }
    return data_dir


def make_site(store: JSONStore, site_id: str) -> Site:
    site = Site(
        site_id=site_id,
        created_at=datetime.now(timezone.utc),
        created_by="test-agent",
        purpose="testing project timestamps",
    )
    store.save_site(site)
    return site


def make_folio(
    store: JSONStore, folio_id: str, site_id: str, created_at: datetime
) -> Folio:
    folio = Folio(
        folio_id=folio_id,
        type="finding",
        site_id=site_id,
        created_at=created_at,
        created_by="test-agent",
        title=f"Folio {folio_id} title for testing",
        content="Test content for projects timestamps endpoint",
        status="open",
        metadata={},
    )
    store.save_folio(folio)
    return folio


class TestProjectsTimestamps:
    def test_empty_registry_returns_empty_dict(self, client, fake_registry):
        resp = client.get("/skein/projects/timestamps")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_returns_max_across_sites_per_project(
        self, client, fake_registry, projects_root
    ):
        # Project alpha: two sites, three folios. Latest is t3.
        register_project(fake_registry, projects_root, "alpha")
        alpha_store = make_project_store(projects_root, "alpha")
        make_site(alpha_store, "site-1")
        make_site(alpha_store, "site-2")
        t1 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
        make_folio(alpha_store, "f-1", "site-1", t1)
        make_folio(alpha_store, "f-2", "site-2", t3)
        make_folio(alpha_store, "f-3", "site-1", t2)

        # Project beta: one site, one folio at tb.
        register_project(fake_registry, projects_root, "beta")
        beta_store = make_project_store(projects_root, "beta")
        make_site(beta_store, "only-site")
        tb = datetime(2026, 2, 14, 9, 30, tzinfo=timezone.utc)
        make_folio(beta_store, "f-b", "only-site", tb)

        resp = client.get("/skein/projects/timestamps")
        assert resp.status_code == 200
        assert resp.json() == {
            "alpha": int(t3.timestamp()),
            "beta": int(tb.timestamp()),
        }

    def test_values_are_integers(self, client, fake_registry, projects_root):
        register_project(fake_registry, projects_root, "alpha")
        store = make_project_store(projects_root, "alpha")
        make_site(store, "s")
        make_folio(store, "f-1", "s", datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))

        resp = client.get("/skein/projects/timestamps")
        data = resp.json()
        assert isinstance(data["alpha"], int)

    def test_project_with_missing_db_omitted(
        self, client, fake_registry, projects_root
    ):
        # Register a project but never create its data dir / db.
        data_dir = projects_root / "ghost" / ".skein" / "data"
        fake_registry["registry"]["ghost"] = {
            "path": str(projects_root / "ghost"),
            "data_dir": str(data_dir),
            "name": "ghost",
        }
        # Real project alongside.
        register_project(fake_registry, projects_root, "alpha")
        store = make_project_store(projects_root, "alpha")
        make_site(store, "s")
        make_folio(store, "f-1", "s", datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))

        resp = client.get("/skein/projects/timestamps")
        data = resp.json()
        assert "alpha" in data
        assert "ghost" not in data

    def test_project_with_zero_folios_omitted(
        self, client, fake_registry, projects_root
    ):
        register_project(fake_registry, projects_root, "empty-proj")
        empty_store = make_project_store(projects_root, "empty-proj")
        make_site(empty_store, "site-1")  # site exists but no folios

        register_project(fake_registry, projects_root, "alpha")
        alpha_store = make_project_store(projects_root, "alpha")
        make_site(alpha_store, "s")
        make_folio(
            alpha_store,
            "f-1",
            "s",
            datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        )

        resp = client.get("/skein/projects/timestamps")
        data = resp.json()
        assert "alpha" in data
        assert "empty-proj" not in data

    def test_since_filter_excludes_equal_to_cutoff(
        self, client, fake_registry, projects_root
    ):
        """Strict > semantics: a project whose ts equals the cutoff is excluded."""
        register_project(fake_registry, projects_root, "alpha")
        store = make_project_store(projects_root, "alpha")
        make_site(store, "s")
        t = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        make_folio(store, "f-1", "s", t)

        cutoff = int(t.timestamp())
        resp = client.get(f"/skein/projects/timestamps?since={cutoff}")
        assert resp.json() == {}

    def test_since_filter_excludes_timestamp_below_cutoff(
        self, client, fake_registry, projects_root
    ):
        """timestamp < cutoff: excluded."""
        register_project(fake_registry, projects_root, "alpha")
        store = make_project_store(projects_root, "alpha")
        make_site(store, "s")
        t = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        make_folio(store, "f-1", "s", t)

        cutoff = int(t.timestamp()) + 1  # cutoff is 1s above the project's ts
        resp = client.get(f"/skein/projects/timestamps?since={cutoff}")
        assert resp.json() == {}

    def test_since_filter_includes_timestamp_above_cutoff(
        self, client, fake_registry, projects_root
    ):
        """timestamp > cutoff: included."""
        register_project(fake_registry, projects_root, "alpha")
        store = make_project_store(projects_root, "alpha")
        make_site(store, "s")
        t = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        make_folio(store, "f-1", "s", t)

        cutoff = int(t.timestamp()) - 1  # cutoff is 1s below the project's ts
        resp = client.get(f"/skein/projects/timestamps?since={cutoff}")
        assert resp.json() == {"alpha": int(t.timestamp())}

    def test_new_folio_bumps_project_timestamp(
        self, client, fake_registry, projects_root
    ):
        register_project(fake_registry, projects_root, "alpha")
        store = make_project_store(projects_root, "alpha")
        make_site(store, "s")
        t1 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        make_folio(store, "f-1", "s", t1)

        resp1 = client.get("/skein/projects/timestamps")
        assert resp1.json()["alpha"] == int(t1.timestamp())

        # Add a newer folio in (a different) site of the same project.
        make_site(store, "s2")
        t2 = t1 + timedelta(hours=1)
        make_folio(store, "f-2", "s2", t2)

        resp2 = client.get("/skein/projects/timestamps")
        assert resp2.json()["alpha"] == int(t2.timestamp())

    def test_corrupt_db_does_not_break_response(
        self, client, fake_registry, projects_root
    ):
        # Healthy project.
        register_project(fake_registry, projects_root, "alpha")
        store = make_project_store(projects_root, "alpha")
        make_site(store, "s")
        t = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        make_folio(store, "f-1", "s", t)

        # Corrupt project: register it, create the data dir, write garbage to skein.db.
        corrupt_data_dir = projects_root / "broken" / ".skein" / "data"
        corrupt_data_dir.mkdir(parents=True, exist_ok=True)
        (corrupt_data_dir / "skein.db").write_bytes(b"this is not a sqlite database")
        fake_registry["registry"]["broken"] = {
            "path": str(projects_root / "broken"),
            "data_dir": str(corrupt_data_dir),
            "name": "broken",
        }

        resp = client.get("/skein/projects/timestamps")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"alpha": int(t.timestamp())}
