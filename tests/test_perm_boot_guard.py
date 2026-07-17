"""Permission model — the ingress boot guard refuses a PRE-rev6 corpus (fell-r3 / audit).

A corpus that has not run perm_model_rev6.migrate() (detected by the station_slugs
claimed_by pair) must be refused at boot with a clear remediation, not left to fail at
runtime with a cryptic 'no such column: claimed_by_issuer'.
"""

from __future__ import annotations


import pytest

from skein.ingress import OperatorInvariantError, create_app
from skein.station import Station, StationBootError
from skein.station_store import DB_FILENAME
from tests.test_perm_migration import _old_shape_db

# --- OFF-posture multi-party boot refusal (rev 6 §9) ------------------------
#
# rev 6 §9: OFF has NO edit authorization — single-party/local ONLY; a multi-party
# station MUST run ON. create_app refuses OFF boot when > 1 identity is bound. The
# ">1 active binding" predicate is the faithful reading of "single-party" (account_bindings
# PRIMARY KEY (issuer, subject) makes one active binding == one distinct party); it is
# stricter than "non-operator bindings > 0" — two operator bindings are still two parties.

ISSUER = "did:key:zTEST-issuer"


def _seed_station(data_dir, bindings):
    """Birth a fresh rev-6 station at ``data_dir`` seeded with ``bindings`` — a list of
    (subject, role) tuples — committing and closing so ``create_app`` opens it fresh."""
    s = Station(data_dir)
    try:
        for subject, role in bindings:
            s.store.add_binding(
                ISSUER, subject, role=role,
                vouched_by_issuer=ISSUER, vouched_by_subject=ISSUER,
            )
    finally:
        s.close()


def _boot_env(monkeypatch, data_dir, *, require_signed):
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(data_dir))
    if require_signed:
        monkeypatch.setenv("SKEIN_STATION_REQUIRE_SIGNED", "1")
    else:
        monkeypatch.setenv("SKEIN_STATION_REQUIRE_SIGNED", "0")
    # A stray origin in the real env would raise StationEnvError past the guard we test.
    monkeypatch.delenv("SKEIN_STATION_ORIGIN", raising=False)


def test_off_refuses_multi_party_second_binding(tmp_path, monkeypatch):
    # operator + a second (non-operator) binding => 2 parties => multi-party.
    data_dir = tmp_path / ".skein-station"
    _seed_station(data_dir, [("op@x", "operator"), ("author@x", "originator")])
    _boot_env(monkeypatch, data_dir, require_signed=False)
    with pytest.raises(StationBootError) as ei:
        create_app()
    msg = str(ei.value)
    assert "multi-party" in msg
    assert "SKEIN_STATION_REQUIRE_SIGNED" in msg


def test_off_refuses_two_operators(tmp_path, monkeypatch):
    # Pins the stricter predicate: two operator bindings (0 non-operator) is still
    # multi-party and MUST be refused under OFF. The weaker "non-operator > 0" would boot.
    data_dir = tmp_path / ".skein-station"
    _seed_station(data_dir, [("op1@x", "operator"), ("op2@x", "operator")])
    _boot_env(monkeypatch, data_dir, require_signed=False)
    with pytest.raises(StationBootError) as ei:
        create_app()
    assert "multi-party" in str(ei.value)


def test_off_boots_single_operator(tmp_path, monkeypatch):
    # A lone operator is single-party — local single-party dev MUST keep booting under OFF.
    data_dir = tmp_path / ".skein-station"
    _seed_station(data_dir, [("op@x", "operator")])
    _boot_env(monkeypatch, data_dir, require_signed=False)
    assert create_app() is not None


def test_off_boots_zero_binding(tmp_path, monkeypatch):
    # A pristine, unbound station is single-party/local — MUST keep booting under OFF.
    data_dir = tmp_path / ".skein-station"
    _seed_station(data_dir, [])
    _boot_env(monkeypatch, data_dir, require_signed=False)
    assert create_app() is not None


def test_on_single_operator_invariant_unchanged(tmp_path, monkeypatch):
    # ON path is untouched by the OFF guard: a lone operator boots; two operators still
    # trip the pre-existing single-active-operator invariant (OperatorInvariantError, NOT
    # the new multi-party StationBootError).
    single = tmp_path / "single" / ".skein-station"
    _seed_station(single, [("op@x", "operator")])
    _boot_env(monkeypatch, single, require_signed=True)
    assert create_app() is not None

    two = tmp_path / "two" / ".skein-station"
    _seed_station(two, [("op1@x", "operator"), ("op2@x", "operator")])
    _boot_env(monkeypatch, two, require_signed=True)
    with pytest.raises(OperatorInvariantError) as ei:
        create_app()
    assert "operator" in str(ei.value)


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
