"""Permission model — the ingress boot guard refuses a PRE-rev6 corpus (fell-r3 / audit).

A corpus that has not run perm_model_rev6.migrate() (detected by the station_slugs
claimed_by pair) must be refused at boot with a clear remediation, not left to fail at
runtime with a cryptic 'no such column: claimed_by_issuer'.
"""

from __future__ import annotations


import pytest

from skein.ingress import create_app
from skein.station import Station, StationBootError
from skein.station_store import DB_FILENAME
from tests.test_perm_migration import _old_shape_db


def test_perm_schema_current_detects_shape(tmp_path):
    fresh = Station(tmp_path / "fresh" / ".skein-station")
    try:
        assert fresh.store.perm_schema_current() is True
    finally:
        fresh.close()


def test_boot_refuses_pre_rev6_corpus(tmp_path, monkeypatch):
    data_dir = tmp_path / ".skein-station"
    data_dir.mkdir()
    _old_shape_db(data_dir / DB_FILENAME)  # a pre-rev6 station_slugs shape
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(data_dir))
    monkeypatch.delenv("SKEIN_STATION_REQUIRE_SIGNED", raising=False)
    with pytest.raises(StationBootError) as ei:
        create_app()
    assert "perm_model_rev6" in str(ei.value)


def test_boot_accepts_after_migration(tmp_path, monkeypatch):
    from skein.migrations.perm_model_rev6 import migrate

    data_dir = tmp_path / ".skein-station"
    data_dir.mkdir()
    db = data_dir / DB_FILENAME
    _old_shape_db(db)
    # migrating flips the corpus to the rev-6 shape (and quarantines the merge)...
    migrate(db)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(data_dir))
    monkeypatch.delenv("SKEIN_STATION_REQUIRE_SIGNED", raising=False)
    # ...so the guard no longer refuses (any later error is not the perm-schema guard)
    try:
        create_app()
    except StationBootError as e:
        assert "perm_model_rev6" not in str(e)
