"""The station launchers on the ``skein station`` group (design §5 Stage 6).

Re-homed from skein_next/cli.py's ``serve`` / ``ingress`` verbs (the live launch
path's two points: the read Dockerfile CMD and the ingress compose ``command:``,
repointed at Stage 7b). The verbs stay thin: plumb ``--data-dir`` into the
station env (NEW key), lazy-import the server module, hand host/port to its
``run_server``. The ``maintenance verify-cache`` launcher is exercised end-to-end
by VC9 in test_station_cli_account.py.
"""

from __future__ import annotations

import os

from click.testing import CliRunner

from client.cli import cli


def test_serve_launches_web_run_server(tmp_path, monkeypatch):
    calls = {}

    def fake_run_server(host="127.0.0.1", port=9001):
        calls["host"], calls["port"] = host, port
        calls["data_dir"] = os.environ.get("SKEIN_STATION_DATA_DIR")

    from skein.web import app as web_app

    monkeypatch.setattr(web_app, "run_server", fake_run_server)
    r = CliRunner().invoke(
        cli,
        ["station", "--data-dir", str(tmp_path / "corpus"), "serve",
         "--host", "0.0.0.0", "--port", "9009"],
    )
    assert r.exit_code == 0, r.output
    assert (calls["host"], calls["port"]) == ("0.0.0.0", 9009)
    assert calls["data_dir"] == str(tmp_path / "corpus")


def test_ingress_launches_ingress_run_server(tmp_path, monkeypatch):
    calls = {}

    def fake_run_server(host="127.0.0.1", port=9101):
        calls["host"], calls["port"] = host, port
        calls["data_dir"] = os.environ.get("SKEIN_STATION_DATA_DIR")

    from skein import ingress as ingress_mod

    monkeypatch.setattr(ingress_mod, "run_server", fake_run_server)
    r = CliRunner().invoke(
        cli,
        ["station", "--data-dir", str(tmp_path / "corpus"), "ingress",
         "--host", "127.0.0.1", "--port", "9111"],
    )
    assert r.exit_code == 0, r.output
    assert (calls["host"], calls["port"]) == ("127.0.0.1", 9111)
    assert calls["data_dir"] == str(tmp_path / "corpus")


def test_default_ports_match_live_surfaces(tmp_path, monkeypatch):
    """serve defaults to the read surface's :9001, ingress to the write :9101 —
    the two live ports the Stage-7b repoint relies on."""
    seen = {}

    from skein.web import app as web_app
    from skein import ingress as ingress_mod

    monkeypatch.setattr(
        web_app, "run_server", lambda host="127.0.0.1", port=9001: seen.setdefault("serve", port)
    )
    monkeypatch.setattr(
        ingress_mod,
        "run_server",
        lambda host="127.0.0.1", port=9101: seen.setdefault("ingress", port),
    )
    d = str(tmp_path / "corpus")
    assert CliRunner().invoke(cli, ["station", "--data-dir", d, "serve"]).exit_code == 0
    assert CliRunner().invoke(cli, ["station", "--data-dir", d, "ingress"]).exit_code == 0
    assert seen == {"serve": 9001, "ingress": 9101}


def test_data_dir_defaults_from_station_env(tmp_path, monkeypatch):
    """Without --data-dir the group resolves the station env key, so the ops
    verbs and the launchers agree on the corpus location."""
    from skein.station import Station

    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / "envdir"))
    r = CliRunner().invoke(
        cli,
        ["station", "account", "init-operator", "--issuer", "https://i", "--subject", "s"],
    )
    assert r.exit_code == 0, r.output
    with Station(tmp_path / "envdir") as st:
        assert st.store.get_operator() is not None
