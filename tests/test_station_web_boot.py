"""Read-app boot readiness — the missing/corrupt-db refusal (brief-20260712-t1tf #4).

The read surface's stores are PER-REQUEST (``get_store``), so before the boot
probe a missing or corrupt corpus booted "fine" and then 500ed every request —
the operator's first signal was a raw sqlite3 traceback in the request log, not
a refusal at startup. Now ``create_app`` opens a StationStore once read-only,
does one trivial read, closes, and refuses startup with the typed
``StationBootError`` every entry point presents as a clean one-line exit:
``python -m skein.web`` (SystemExit 2) and the ``skein station serve`` launcher
(ClickException). Unlike the ingress, a MISSING db here IS a fault — a read-only
server with nothing to serve — and the probe must never create one (the ro
open's no-write contract; a 0-byte skein.db left behind would then boot forever
after as a corrupt corpus).
"""

from __future__ import annotations

import pytest

from skein.station import Station, StationBootError
from skein.web import __main__ as webmain
from skein.web import app as webapp


def _wire_env(tmp_path, monkeypatch, *, name="probe-station"):
    d = tmp_path / ".skein-station"
    monkeypatch.setenv(webapp.ENV_DATA_DIR, str(d))
    # A name via env keeps the stationfile gate (StationfileError) out of the
    # frame: these tests pin the CORPUS probe, the config gate has its own suite.
    monkeypatch.setenv(webapp.ENV_NAME, name)
    return d


def _corrupt_db(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "skein.db").write_bytes(b"this is not a sqlite database\n" * 8)


def test_missing_db_boot_refusal(tmp_path, monkeypatch):
    d = _wire_env(tmp_path, monkeypatch)
    d.mkdir()
    with pytest.raises(StationBootError) as exc:
        webapp.create_app()
    assert str(d / "skein.db") in str(exc.value)


def test_boot_probe_never_creates_a_db(tmp_path, monkeypatch):
    d = _wire_env(tmp_path, monkeypatch)
    d.mkdir()
    with pytest.raises(StationBootError):
        webapp.create_app()
    assert not (d / "skein.db").exists()


def test_corrupt_db_boot_refusal(tmp_path, monkeypatch):
    d = _wire_env(tmp_path, monkeypatch)
    _corrupt_db(d)
    with pytest.raises(StationBootError) as exc:
        webapp.create_app()
    assert str(d / "skein.db") in str(exc.value)


def test_db_path_directory_boot_refusal(tmp_path, monkeypatch):
    d = _wire_env(tmp_path, monkeypatch)
    (d / "skein.db").mkdir(parents=True)
    with pytest.raises(StationBootError):
        webapp.create_app()


def test_unset_data_dir_boot_refusal(monkeypatch):
    monkeypatch.delenv("SKEIN_STATION_DATA_DIR", raising=False)
    monkeypatch.setenv(webapp.ENV_NAME, "probe-station")
    with pytest.raises(StationBootError) as exc:
        webapp.create_app()
    assert webapp.ENV_DATA_DIR in str(exc.value)


def test_healthy_empty_corpus_boots(tmp_path, monkeypatch):
    """An EMPTY station corpus is servable (the probe's trivial read must not
    demand content) — only an unopenable/unreadable one refuses."""
    d = _wire_env(tmp_path, monkeypatch)
    Station(d).close()  # births a valid, empty station corpus
    assert webapp.create_app() is not None


def test_module_entry_missing_db_exits_2(tmp_path, monkeypatch, capsys):
    """`python -m skein.web` presents the boot fault as a one-line stderr message
    + SystemExit 2 — StationBootError propagates out of run_server to THIS
    wrapper (presentation lives at the entry points, deep_code_audit fell r4)."""
    d = _wire_env(tmp_path, monkeypatch)
    d.mkdir()
    with pytest.raises(SystemExit) as exc:
        webmain.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "station will not start" in err and "skein.db" in err
    assert "Traceback" not in err


def test_launcher_corrupt_db_clean_error(tmp_path, monkeypatch):
    """`skein station serve` over a corrupt db exits via ClickException — the
    one-line 'Error: …' presentation, never an escaped sqlite3 traceback."""
    from click.testing import CliRunner

    from client.cli import cli

    d = tmp_path / "corpus"
    _corrupt_db(d)
    monkeypatch.setenv(webapp.ENV_NAME, "probe-station")
    monkeypatch.setenv(webapp.ENV_DATA_DIR, str(d))  # restored even though the launcher exports it
    result = CliRunner().invoke(cli, ["station", "--data-dir", str(d), "serve"])
    assert result.exit_code != 0
    assert "Error:" in result.output and "skein.db" in result.output
    assert "Traceback" not in result.output
