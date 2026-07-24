"""Tests for the CLI's URL resolution (client/cli.py resolve_base_url).

The client's ladder used to bottom out on a hardcoded http://localhost:8001
while the service resolved its bind address through skein.service_address, so
SKEIN_PORT moved the service and stranded every command (notion-20260722-95kl).
Now the client's floor IS the machine's service address, and a server_url
holding the exact literal `skein init` used to write reads as absent — 51 of 51
registered project configs on the machine that motivated this held that
literal, so honoring it would have shadowed the shared floor forever.

get_base_url runs on every command: these tests also pin that no bad VALUE
(unparseable SKEIN_PORT, malformed server.json) ever raises out of it. An
unprobeable project/global config still raises for normal commands — that
loudness is deliberate and pinned in test_doctor.py's TestSealedGlobalConfig.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from client.cli import get_base_url, resolve_base_url

LEGACY = "http://localhost:8001"
LEGACY_127 = "http://127.0.0.1:8001"


@pytest.fixture
def bare_machine(tmp_path, monkeypatch):
    """No flag, no env URL, no configs anywhere: resolution must reach the
    floor. Returns the SKEIN_HOME path so tests can drop a server.json in."""
    monkeypatch.delenv("SKEIN_URL", raising=False)
    monkeypatch.delenv("SKEIN_PROJECT", raising=False)
    monkeypatch.delenv("SKEIN_PORT", raising=False)
    monkeypatch.delenv("SKEIN_HOST", raising=False)
    monkeypatch.delenv("SKEIN_SERVER_CONFIG", raising=False)
    home = tmp_path / "skein-home"
    home.mkdir()
    monkeypatch.setenv("SKEIN_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))  # no ~/.skein
    outside = tmp_path / "outside-any-project"
    outside.mkdir()
    monkeypatch.chdir(outside)
    return home


@pytest.fixture
def in_project(tmp_path, monkeypatch):
    """A project whose .skein/config.json the test writes. Returns the config
    path. Layered on top of bare_machine by requesting both."""
    project = tmp_path / "proj"
    (project / ".skein").mkdir(parents=True)
    monkeypatch.chdir(project)
    return project / ".skein" / "config.json"


def write_config(path: Path, server_url):
    path.write_text(json.dumps({"project_id": "proj", "server_url": server_url}))


class TestUpperRungs:
    def test_url_flag_wins(self, bare_machine):
        r = resolve_base_url("http://example:9000/")
        assert r.url == "http://example:9000"
        assert r.source == "--url flag"

    def test_skein_url_wins_without_a_flag(self, bare_machine, monkeypatch):
        monkeypatch.setenv("SKEIN_URL", "http://example:9001/")
        r = resolve_base_url()
        assert r.url == "http://example:9001"
        assert r.source == "SKEIN_URL"

    def test_a_deliberate_project_server_url_is_honored(
        self, bare_machine, in_project, monkeypatch
    ):
        write_config(in_project, "http://elsewhere:8200")
        r = resolve_base_url()
        assert r.url == "http://elsewhere:8200"
        assert "project" in r.source

    def test_a_deliberate_global_server_url_is_honored(self, bare_machine, tmp_path, monkeypatch):
        dot_skein = tmp_path / "user-home" / ".skein"
        dot_skein.mkdir(parents=True)
        (dot_skein / "config.json").write_text(json.dumps({"server_url": "http://global:8300"}))
        r = resolve_base_url()
        assert r.url == "http://global:8300"
        assert "~/.skein" in r.source


class TestLegacyLiteralExclusion:
    @pytest.mark.parametrize("literal", [LEGACY, LEGACY_127, LEGACY + "/", LEGACY_127 + "/"])
    def test_project_legacy_literal_reads_as_absent(
        self, bare_machine, in_project, literal
    ):
        write_config(in_project, literal)
        r = resolve_base_url()
        assert r.url == "http://127.0.0.1:8001"
        assert r.source.startswith("local service address")
        assert any("retired default" in note for note in r.ignored)

    def test_global_legacy_literal_reads_as_absent(self, bare_machine, tmp_path):
        dot_skein = tmp_path / "user-home" / ".skein"
        dot_skein.mkdir(parents=True)
        (dot_skein / "config.json").write_text(json.dumps({"server_url": LEGACY}))
        r = resolve_base_url()
        assert r.source.startswith("local service address")
        assert any("~/.skein" in note for note in r.ignored)

    def test_the_ignored_literal_uncovers_the_moved_floor(
        self, bare_machine, in_project, monkeypatch
    ):
        """The whole point: a pre-existing init-written config no longer pins
        the port, so SKEIN_PORT moves the client too."""
        write_config(in_project, LEGACY)
        monkeypatch.setenv("SKEIN_PORT", "8123")
        assert get_base_url() == "http://127.0.0.1:8123"

    def test_a_nearby_but_different_url_is_not_excluded(self, bare_machine, in_project):
        write_config(in_project, "http://localhost:8002")
        assert get_base_url() == "http://localhost:8002"


class TestFloorIsTheServiceAddress:
    def test_bare_default(self, bare_machine):
        r = resolve_base_url()
        assert r.url == "http://127.0.0.1:8001"
        assert r.source == "local service address (built-in default)"

    def test_skein_port_moves_the_floor(self, bare_machine, monkeypatch):
        monkeypatch.setenv("SKEIN_PORT", "8123")
        r = resolve_base_url()
        assert r.url == "http://127.0.0.1:8123"
        assert "SKEIN_PORT" in r.source

    def test_server_json_moves_the_floor(self, bare_machine):
        (bare_machine / "server.json").write_text(json.dumps({"port": 8155}))
        r = resolve_base_url()
        assert r.url == "http://127.0.0.1:8155"
        assert "server.json" in r.source

    def test_client_and_server_agree_wherever_the_config_points(
        self, bare_machine, monkeypatch
    ):
        """The invariant the whole change exists for, checked at both ends."""
        from skein.server import get_config

        (bare_machine / "server.json").write_text(json.dumps({"port": 8155}))
        monkeypatch.setenv("SKEIN_PORT", "8156")
        config = get_config()
        assert get_base_url() == f"http://127.0.0.1:{config['port']}"
        assert config["port"] == 8156

    def test_a_wildcard_bind_maps_to_loopback(self, bare_machine):
        (bare_machine / "server.json").write_text(
            json.dumps({"host": "0.0.0.0", "port": 8155})
        )
        assert get_base_url() == "http://127.0.0.1:8155"

    def test_an_ipv6_wildcard_maps_to_bracketed_loopback(self, bare_machine):
        (bare_machine / "server.json").write_text(json.dumps({"host": "::", "port": 8155}))
        assert get_base_url() == "http://[::1]:8155"

    def test_a_concrete_host_is_used_as_given(self, bare_machine):
        (bare_machine / "server.json").write_text(
            json.dumps({"host": "192.168.1.7", "port": 8155})
        )
        assert get_base_url() == "http://192.168.1.7:8155"


class TestNoValueEverRaises:
    """get_base_url runs on every command; a bad VALUE must resolve, not crash.
    (An unprobeable config still raises — pinned in test_doctor.py.)"""

    @pytest.mark.parametrize("bad", ["80o1", "", "-5", "65536", "8 001"])
    def test_a_broken_skein_port_resolves_to_the_default_with_a_problem(
        self, bare_machine, monkeypatch, bad
    ):
        monkeypatch.setenv("SKEIN_PORT", bad)
        r = resolve_base_url()
        assert r.url == "http://127.0.0.1:8001"
        if bad.strip():
            assert any("SKEIN_PORT" in p for p in r.problems)

    def test_a_malformed_server_json_resolves_to_the_default(self, bare_machine):
        (bare_machine / "server.json").write_text("{ not json")
        r = resolve_base_url()
        assert r.url == "http://127.0.0.1:8001"
        assert r.problems

    def test_a_non_string_server_url_in_a_config_is_skipped(
        self, bare_machine, in_project
    ):
        in_project.write_text(json.dumps({"project_id": "proj", "server_url": 8001}))
        assert get_base_url() == "http://127.0.0.1:8001"

    def test_a_broken_skein_server_config_path_is_skipped(self, bare_machine, monkeypatch):
        monkeypatch.setenv("SKEIN_SERVER_CONFIG", "~nosuchuser-xyz/server.json")
        assert get_base_url() == "http://127.0.0.1:8001"


class TestDoctorReportsResolution:
    def test_doctor_names_the_winning_rung(self, bare_machine, monkeypatch):
        from client.cli import doctor_checks

        monkeypatch.setenv("SKEIN_PORT", "8123")
        r = resolve_base_url(tolerant=True)
        checks = [c for c in doctor_checks(r.url, r) if c["name"] == "url resolution"]
        assert checks, "doctor must include a url resolution line"
        assert "SKEIN_PORT" in checks[0]["detail"]
        assert checks[0]["ok"] is True

    def test_doctor_warns_on_an_ignored_value(self, bare_machine, monkeypatch):
        from client.cli import doctor_checks

        monkeypatch.setenv("SKEIN_PORT", "80o1")
        r = resolve_base_url(tolerant=True)
        warns = [
            c
            for c in doctor_checks(r.url, r)
            if c["name"] == "url resolution" and not c["ok"]
        ]
        assert len(warns) == 1
        assert warns[0]["level"] == "warn"
        assert "SKEIN_PORT" in warns[0]["detail"]

    def test_doctor_mentions_an_ignored_legacy_literal(
        self, bare_machine, in_project, monkeypatch
    ):
        from client.cli import doctor_checks

        write_config(in_project, LEGACY)
        r = resolve_base_url(tolerant=True)
        infos = [
            c
            for c in doctor_checks(r.url, r)
            if c["name"] == "url resolution" and c["ok"]
        ]
        assert len(infos) == 1
        assert "retired default" in infos[0]["detail"]


class TestInitStopsPinning:
    def test_init_writes_no_server_url(self, bare_machine, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from client.cli import cli

        fresh = tmp_path / "fresh-project"
        fresh.mkdir()
        monkeypatch.chdir(fresh)
        result = CliRunner().invoke(cli, ["init", "--project", "fresh"])
        assert result.exit_code == 0, result.output
        config = json.loads((fresh / ".skein" / "config.json").read_text())
        assert "server_url" not in config
        assert config["project_id"] == "fresh"
