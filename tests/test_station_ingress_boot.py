"""Ingress startup invariant — require_signed + the single-active-operator gate (D13-D20,
finding-8), re-homed from the create_app cells of skein_next/tests/test_cli_account.py.

These pin the ingress-side boot behavior (gotcha #4 of the Stage-3 brief): under
require_signed, create_app refuses boot unless EXACTLY ONE active operator exists in the
account_bindings sidecar, and an unrecognized SKEIN_NEXT_REQUIRE_SIGNED value refuses boot
rather than silently running open. Operators are seeded directly through the store here —
the operator/invite `account` CLI verbs ride with Stage 5/6 (test_cli_account); this suite
owns only the ingress create_app invariant.

Also pins boot I/O totality (brief-20260712-t1tf #4): an unusable corpus db — garbage
bytes, a directory at the db path, the data dir itself a file — refuses boot with the
typed StationBootError in EITHER posture, presented cleanly by every entry point
(`python -m skein.ingress` SystemExit 2, `skein station ingress` ClickException), never
a raw sqlite3 traceback. A MISSING db is deliberately NOT a fault here: the write
surface's read_write open legitimately births the corpus.
"""

from __future__ import annotations

import logging

import pytest

from skein import ingress
from skein.station import Station, StationBootError

I, S = "https://accounts.google.com", "operator@example.com"


def _seed_operator(data_dir, issuer=I, subject=S):
    st = Station(data_dir)
    try:
        st.store.add_binding(issuer, subject, role="operator",
                             vouched_by_issuer=issuer, vouched_by_subject=subject)
    finally:
        st.close()


def _wire_env(tmp_path, monkeypatch, value):
    monkeypatch.setenv(ingress.ENV_DATA_DIR, str(tmp_path / ".skein-next"))
    if value is None:
        monkeypatch.delenv(ingress.ENV_REQUIRE_SIGNED, raising=False)
    else:
        monkeypatch.setenv(ingress.ENV_REQUIRE_SIGNED, value)
    return tmp_path / ".skein-next"


def test_create_app_require_signed_without_operator_refuses(tmp_path, monkeypatch):  # D13
    d = _wire_env(tmp_path, monkeypatch, "1")
    Station(d).close()  # empty corpus, no operator
    with pytest.raises(ingress.OperatorInvariantError) as exc:
        ingress.create_app()
    assert "account init-operator" in str(exc.value)


def test_create_app_require_signed_with_operator_starts(tmp_path, monkeypatch):  # D14
    d = _wire_env(tmp_path, monkeypatch, "1")
    _seed_operator(d)
    assert ingress.create_app() is not None


def test_require_signed_off_no_operator_ok(tmp_path, monkeypatch):  # D15
    d = _wire_env(tmp_path, monkeypatch, None)
    Station(d).close()
    assert ingress.create_app() is not None


def test_operator_identity_sourced_from_sidecar(tmp_path, monkeypatch):  # D16
    d = _wire_env(tmp_path, monkeypatch, "1")
    _seed_operator(d)
    assert ingress.create_app() is not None
    st = Station(d)
    try:
        assert st.store.get_operator().subject == S
    finally:
        st.close()


def test_create_app_require_signed_multiple_operators_refuses(tmp_path, monkeypatch):  # D20
    d = _wire_env(tmp_path, monkeypatch, "1")
    st = Station(d)
    try:
        st.store.add_binding("https://idpA", "first", role="operator")
        st.store.add_binding("https://idpB", "second", role="operator")
    finally:
        st.close()
    with pytest.raises(ingress.OperatorInvariantError) as exc:
        ingress.create_app()
    assert "single-active-operator" in str(exc.value) or "active operators" in str(exc.value)


def test_ingress_startup_logs_operator_status(tmp_path, monkeypatch, caplog):  # D18
    d = _wire_env(tmp_path, monkeypatch, "1")
    _seed_operator(d)
    with caplog.at_level(logging.INFO, logger="skein.ingress"):
        ingress.create_app()
    assert any("operator" in rec.message for rec in caplog.records)


def test_create_app_require_signed_on_spelling_still_enforces(tmp_path, monkeypatch):  # finding-8
    """A wider truthy spelling (e.g. 'on') must drive the SAME startup invariant as
    '1' — no operator present must still refuse boot, not silently run open."""
    d = _wire_env(tmp_path, monkeypatch, "on")
    Station(d).close()
    with pytest.raises(ingress.OperatorInvariantError):
        ingress.create_app()
    _seed_operator(d)
    assert ingress.create_app() is not None  # with an operator present, boots fine


def test_create_app_require_signed_garbage_value_refuses_boot(tmp_path, monkeypatch):  # finding-8
    """An unrecognized SKEIN_NEXT_REQUIRE_SIGNED value must refuse to boot at all — never
    silently fall back to require_signed=False and accept unsigned content wide open."""
    _wire_env(tmp_path, monkeypatch, "onn")
    with pytest.raises(ingress.RequireSignedConfigError):
        ingress.create_app()


# --- boot I/O totality (brief-20260712-t1tf #4) --------------------------------


def _corrupt_db(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "skein.db").write_bytes(b"this is not a sqlite database\n" * 8)


def test_corrupt_db_boot_refusal(tmp_path, monkeypatch):
    """Garbage bytes at the db path are a typed StationBootError naming the path —
    the raw `sqlite3.DatabaseError: file is not a database` this class escaped as."""
    d = _wire_env(tmp_path, monkeypatch, "1")
    _corrupt_db(d)
    with pytest.raises(StationBootError) as exc:
        ingress.create_app()
    assert str(d / "skein.db") in str(exc.value)


def test_corrupt_db_boot_refusal_rs_off(tmp_path, monkeypatch):
    """The probe runs in EITHER posture: under OFF the pre-fix corpus touch was
    per-request only, so a corrupt db booted 'fine' and 500ed the first publish."""
    d = _wire_env(tmp_path, monkeypatch, None)
    _corrupt_db(d)
    with pytest.raises(StationBootError):
        ingress.create_app()


def test_db_path_directory_boot_refusal(tmp_path, monkeypatch):
    d = _wire_env(tmp_path, monkeypatch, "1")
    (d / "skein.db").mkdir(parents=True)
    with pytest.raises(StationBootError) as exc:
        ingress.create_app()
    assert str(d / "skein.db") in str(exc.value)


def test_data_dir_is_file_boot_refusal(tmp_path, monkeypatch):
    """OSError on the data dir (mkdir over a file) is the same fault class."""
    d = _wire_env(tmp_path, monkeypatch, "1")
    d.write_text("a file where the data dir should be")
    with pytest.raises(StationBootError):
        ingress.create_app()


def test_unset_data_dir_boot_refusal(monkeypatch):
    monkeypatch.delenv(ingress.ENV_DATA_DIR, raising=False)
    monkeypatch.delenv(ingress.ENV_REQUIRE_SIGNED, raising=False)
    with pytest.raises(StationBootError) as exc:
        ingress.create_app()
    assert ingress.ENV_DATA_DIR in str(exc.value)


def test_missing_db_rs_off_boots_and_creates(tmp_path, monkeypatch):
    """A MISSING db is not a fault on the write surface: the boot open births the
    corpus exactly as the first request's per-request open always has."""
    d = _wire_env(tmp_path, monkeypatch, None)
    assert not (d / "skein.db").exists()
    assert ingress.create_app() is not None
    assert (d / "skein.db").exists()


def test_module_entry_corrupt_db_exits_2(tmp_path, monkeypatch, capsys):
    """`python -m skein.ingress` presents the boot fault as a one-line stderr
    message + SystemExit 2 — the direct-entry twin of the launcher's
    ClickException (same pattern as the config-error clean exit in
    test_station_env)."""
    d = _wire_env(tmp_path, monkeypatch, "1")
    _corrupt_db(d)
    with pytest.raises(SystemExit) as exc:
        ingress.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ingress will not start" in err and "skein.db" in err
    assert "Traceback" not in err


def test_launcher_corrupt_db_clean_error(tmp_path, monkeypatch):
    """`skein station ingress` over a corrupt db exits via ClickException — the
    one-line 'Error: …' presentation, never an escaped sqlite3 traceback."""
    from click.testing import CliRunner

    from client.cli import cli

    d = tmp_path / "corpus"
    _corrupt_db(d)
    monkeypatch.delenv(ingress.ENV_REQUIRE_SIGNED, raising=False)
    monkeypatch.setenv(ingress.ENV_DATA_DIR, str(d))  # restored even though the launcher exports it
    result = CliRunner().invoke(cli, ["station", "--data-dir", str(d), "ingress"])
    assert result.exit_code != 0
    assert "Error:" in result.output and "skein.db" in result.output
    assert "Traceback" not in result.output
