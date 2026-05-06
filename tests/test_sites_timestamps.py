"""Tests for the GET /sites/timestamps bulk-activity endpoint."""

import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skein_server import app
from skein.models import Folio, Site
from skein.routes import get_project_store
from skein.storage import JSONStore


@pytest.fixture
def store_dir():
    d = Path(tempfile.mkdtemp(prefix="skein_timestamps_test_"))
    yield d
    shutil.rmtree(d)


@pytest.fixture
def store(store_dir):
    return JSONStore(store_dir)


@pytest.fixture
def client(store):
    app.dependency_overrides[get_project_store] = lambda: store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_project_store, None)


def make_site(store: JSONStore, site_id: str) -> Site:
    site = Site(
        site_id=site_id,
        created_at=datetime.now(timezone.utc),
        created_by="test-agent",
        purpose="testing timestamps",
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
        content="Test content for timestamps endpoint",
        status="open",
        metadata={},
    )
    store.save_folio(folio)
    return folio


class TestSitesTimestamps:
    def test_empty_db_returns_empty_dict(self, client):
        resp = client.get("/skein/sites/timestamps")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_returns_latest_per_site(self, client, store):
        make_site(store, "alpha")
        make_site(store, "beta")

        t1 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)

        make_folio(store, "f-1", "alpha", t1)
        make_folio(store, "f-2", "alpha", t3)  # latest in alpha
        make_folio(store, "f-3", "beta", t2)

        resp = client.get("/skein/sites/timestamps")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "alpha": int(t3.timestamp()),
            "beta": int(t2.timestamp()),
        }

    def test_values_are_integers(self, client, store):
        make_site(store, "alpha")
        t = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        make_folio(store, "f-1", "alpha", t)

        resp = client.get("/skein/sites/timestamps")
        data = resp.json()
        assert isinstance(data["alpha"], int)

    def test_sites_with_zero_folios_omitted(self, client, store):
        make_site(store, "has-folios")
        make_site(store, "empty-site")

        t = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        make_folio(store, "f-1", "has-folios", t)

        resp = client.get("/skein/sites/timestamps")
        data = resp.json()
        assert "has-folios" in data
        assert "empty-site" not in data

    def test_since_filter_strict_greater_than(self, client, store):
        """?since=<unix> uses strict > (not >=). Boundary: equal is excluded."""
        make_site(store, "alpha")
        make_site(store, "beta")
        make_site(store, "gamma")

        t_low = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        t_mid = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        t_high = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)

        make_folio(store, "f-1", "alpha", t_low)
        make_folio(store, "f-2", "beta", t_mid)
        make_folio(store, "f-3", "gamma", t_high)

        cutoff = int(t_mid.timestamp())

        resp = client.get(f"/skein/sites/timestamps?since={cutoff}")
        data = resp.json()

        # alpha (t_low) excluded; beta (t_mid, equal to cutoff) excluded;
        # gamma (t_high) included.
        assert data == {"gamma": int(t_high.timestamp())}

    def test_since_filter_just_below_includes_boundary(self, client, store):
        make_site(store, "alpha")
        t = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        make_folio(store, "f-1", "alpha", t)

        cutoff = int(t.timestamp()) - 1
        resp = client.get(f"/skein/sites/timestamps?since={cutoff}")
        assert resp.json() == {"alpha": int(t.timestamp())}

    def test_since_filter_just_above_excludes(self, client, store):
        make_site(store, "alpha")
        t = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        make_folio(store, "f-1", "alpha", t)

        cutoff = int(t.timestamp()) + 1
        resp = client.get(f"/skein/sites/timestamps?since={cutoff}")
        assert resp.json() == {}

    def test_new_folio_bumps_site_timestamp(self, client, store):
        make_site(store, "alpha")
        t1 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        make_folio(store, "f-1", "alpha", t1)

        resp1 = client.get("/skein/sites/timestamps")
        assert resp1.json()["alpha"] == int(t1.timestamp())

        t2 = t1 + timedelta(hours=1)
        make_folio(store, "f-2", "alpha", t2)

        resp2 = client.get("/skein/sites/timestamps")
        assert resp2.json()["alpha"] == int(t2.timestamp())
