"""Tests for SKEIN service configuration loading.

The service moved into the package (skein.server) so a wheel install can run it;
`skein_server.py` at the repo root is now a launcher that re-exports it. These
tests target the package module and cover the search order a wheel user depends
on: an explicit override, then <SKEIN_HOME>/server.json, then the repo config.

The ladder itself lives in skein.service_address (skein.server re-exports it):
the CLI's URL resolution bottoms out on the same answer, so this ladder is also
what moves every `skein` command when the service moves. That is why the value
guards below insist on ignore-never-raise: a typo'd SKEIN_PORT reaching
get_base_url must not turn every command into a traceback
(notion-20260722-95kl).
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from skein.server import get_config


@pytest.fixture
def no_config_file(monkeypatch, tmp_path):
    """Remove every config source so the built-in defaults are observable."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path / "skein-home"))
    monkeypatch.delenv("SKEIN_SERVER_CONFIG", raising=False)
    monkeypatch.setattr("skein.service_address.REPO_CONFIG_PATH", tmp_path / "absent" / "config.json")


@pytest.fixture
def clean_env(monkeypatch):
    """Strip SKEIN_* env vars so defaults are observable."""
    for var in ("SKEIN_HOST", "SKEIN_PORT", "SKEIN_LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)


class TestServerHostDefault:
    """Default bind host should be loopback unless explicitly opted out."""

    def test_default_host_is_loopback(self, clean_env, no_config_file):
        """With no env vars and no config file, host defaults to 127.0.0.1."""
        config = get_config()
        assert config["host"] == "127.0.0.1"

    def test_default_port_and_log_level(self, clean_env, no_config_file):
        config = get_config()
        assert config["port"] == 8001
        assert config["log_level"] == "info"

    def test_skein_host_env_overrides_to_all_interfaces(
        self, clean_env, no_config_file, monkeypatch
    ):
        """Operators can opt in to network exposure via SKEIN_HOST=0.0.0.0."""
        monkeypatch.setenv("SKEIN_HOST", "0.0.0.0")
        config = get_config()
        assert config["host"] == "0.0.0.0"

    def test_config_file_overrides_default(self, clean_env, no_config_file, monkeypatch, tmp_path):
        """A config.json with a server.host value takes precedence over the default."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"server": {"host": "0.0.0.0"}}))
        monkeypatch.setattr("skein.service_address.REPO_CONFIG_PATH", config_file)

        config = get_config()
        assert config["host"] == "0.0.0.0"

    def test_env_var_beats_config_file(self, clean_env, no_config_file, monkeypatch, tmp_path):
        """SKEIN_HOST takes precedence over a config.json value."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"server": {"host": "10.0.0.5"}}))
        monkeypatch.setattr("skein.service_address.REPO_CONFIG_PATH", config_file)
        monkeypatch.setenv("SKEIN_HOST", "192.168.1.10")

        config = get_config()
        assert config["host"] == "192.168.1.10"


class TestConfigSearchOrder:
    """A wheel install has no repo config, so the other sources have to work."""

    def test_skein_home_server_json_is_read(self, clean_env, no_config_file, tmp_path, monkeypatch):
        home = tmp_path / "skein-home"
        home.mkdir(parents=True, exist_ok=True)
        # A bare mapping, so a user does not have to know about the "server" key.
        (home / "server.json").write_text(json.dumps({"port": 8123}))

        config = get_config()
        assert config["port"] == 8123

    def test_explicit_override_wins_over_skein_home(
        self, clean_env, no_config_file, tmp_path, monkeypatch
    ):
        home = tmp_path / "skein-home"
        home.mkdir(parents=True, exist_ok=True)
        (home / "server.json").write_text(json.dumps({"port": 8123}))
        override = tmp_path / "elsewhere.json"
        override.write_text(json.dumps({"port": 9999}))
        monkeypatch.setenv("SKEIN_SERVER_CONFIG", str(override))

        config = get_config()
        assert config["port"] == 9999

    def test_a_malformed_config_falls_back_to_defaults(
        self, clean_env, no_config_file, tmp_path, monkeypatch
    ):
        """A broken config file must not keep the service from booting."""
        override = tmp_path / "broken.json"
        override.write_text("{ not json")
        monkeypatch.setenv("SKEIN_SERVER_CONFIG", str(override))

        config = get_config()
        assert config["host"] == "127.0.0.1"
        assert config["port"] == 8001

    def test_unknown_keys_are_ignored(self, clean_env, no_config_file, tmp_path, monkeypatch):
        override = tmp_path / "extra.json"
        override.write_text(json.dumps({"server": {"port": 8222, "nonsense": True}}))
        monkeypatch.setenv("SKEIN_SERVER_CONFIG", str(override))

        config = get_config()
        assert config["port"] == 8222
        assert "nonsense" not in config

    def test_an_install_does_not_read_a_squatting_repo_config(
        self, clean_env, no_config_file, tmp_path, monkeypatch
    ):
        """In a wheel, skein/ sits in site-packages and the repo config path points
        at site-packages/config/config.json — which another package could create.
        It must be consulted only from a genuine checkout (a sibling pyproject.toml)."""
        from skein import server

        rogue_root = tmp_path / "site-packages"
        (rogue_root / "config").mkdir(parents=True)
        (rogue_root / "config" / "config.json").write_text(
            json.dumps({"server": {"host": "0.0.0.0", "port": 45678}})
        )
        # No pyproject.toml beside it -> not a checkout.
        monkeypatch.setattr("skein.service_address.REPO_ROOT", rogue_root)
        monkeypatch.setattr("skein.service_address.REPO_CONFIG_PATH", rogue_root / "config" / "config.json")

        assert server.is_source_checkout() is False
        config = get_config()
        assert config["host"] == "127.0.0.1"
        assert config["port"] == 8001

    def test_an_installed_package_ignores_the_repo_config_even_with_a_planted_pyproject(
        self, clean_env, no_config_file, tmp_path, monkeypatch
    ):
        """A site-packages layout is an install however many files are planted
        beside the package — a stray pyproject.toml there does not make it a
        checkout, so the rogue config stays ignored."""
        from skein import server

        rogue_root = tmp_path / "env" / "lib" / "python3.12" / "site-packages"
        (rogue_root / "config").mkdir(parents=True)
        (rogue_root / "config" / "config.json").write_text(
            json.dumps({"server": {"host": "0.0.0.0", "port": 45678}})
        )
        (rogue_root / "pyproject.toml").write_text("[project]\nname='squatter'\n")
        monkeypatch.setattr("skein.service_address.REPO_ROOT", rogue_root)
        monkeypatch.setattr("skein.service_address.REPO_CONFIG_PATH", rogue_root / "config" / "config.json")

        assert server.is_source_checkout() is False
        config = get_config()
        assert config["host"] == "127.0.0.1"
        assert config["port"] == 8001

    def test_a_checkout_still_reads_its_repo_config(
        self, clean_env, no_config_file, tmp_path, monkeypatch
    ):
        from skein import server

        root = tmp_path / "checkout"
        (root / "config").mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname='interskein'\n")
        (root / "config" / "config.json").write_text(json.dumps({"server": {"port": 8321}}))
        monkeypatch.setattr("skein.service_address.REPO_ROOT", root)
        monkeypatch.setattr("skein.service_address.REPO_CONFIG_PATH", root / "config" / "config.json")

        assert server.is_source_checkout() is True
        assert get_config()["port"] == 8321


class TestValueGuards:
    """Invalid values are ignored with a problem recorded, never a raise."""

    def test_unparseable_skein_port_is_ignored(self, clean_env, no_config_file, monkeypatch):
        monkeypatch.setenv("SKEIN_PORT", "80o1")
        assert get_config()["port"] == 8001

    @pytest.mark.parametrize("bad", ["0", "-1", "65536", "", " "])
    def test_out_of_range_or_empty_skein_port_is_ignored(
        self, clean_env, no_config_file, monkeypatch, bad
    ):
        monkeypatch.setenv("SKEIN_PORT", bad)
        assert get_config()["port"] == 8001

    def test_a_string_port_in_a_config_file_is_coerced(
        self, clean_env, no_config_file, tmp_path, monkeypatch
    ):
        override = tmp_path / "string-port.json"
        override.write_text(json.dumps({"port": "8123"}))
        monkeypatch.setenv("SKEIN_SERVER_CONFIG", str(override))
        assert get_config()["port"] == 8123

    @pytest.mark.parametrize("bad", ["abc", True, None, [8123], 65536])
    def test_an_unusable_file_port_is_ignored_and_recorded(
        self, clean_env, no_config_file, tmp_path, monkeypatch, bad
    ):
        from skein.service_address import resolve_service_config

        override = tmp_path / "bad-port.json"
        override.write_text(json.dumps({"port": bad, "host": "10.0.0.5"}))
        monkeypatch.setenv("SKEIN_SERVER_CONFIG", str(override))
        resolved = resolve_service_config()
        # The bad value is ignored; the good value in the same file still lands.
        assert resolved.config["port"] == 8001
        assert resolved.config["host"] == "10.0.0.5"
        assert any("port" in p for p in resolved.problems)

    def test_resolution_never_raises_on_a_sealed_skein_home(
        self, clean_env, no_config_file, tmp_path, monkeypatch
    ):
        """The CLI resolves through this on every command, so even a home
        whose very probe raises must resolve to defaults, recorded for
        doctor — not crash `skein sites`."""
        if os.geteuid() == 0:
            pytest.skip("root ignores mode bits")
        from skein.service_address import resolve_service_config

        sealed = tmp_path / "sealed"
        sealed.mkdir()
        sealed.chmod(0)
        monkeypatch.setenv("SKEIN_HOME", str(sealed))
        try:
            resolved = resolve_service_config()
        finally:
            sealed.chmod(0o700)
        assert resolved.config["port"] == 8001
        assert resolved.problems


class TestProvenance:
    """resolve_service_config says where each value came from; doctor prints it."""

    def test_defaults_are_labeled_default(self, clean_env, no_config_file):
        from skein.service_address import resolve_service_config

        resolved = resolve_service_config()
        assert resolved.sources == {"host": "default", "port": "default", "log_level": "default"}
        assert resolved.problems == []

    def test_env_and_file_are_named(self, clean_env, no_config_file, tmp_path, monkeypatch):
        from skein.service_address import resolve_service_config

        home = tmp_path / "skein-home"
        home.mkdir(parents=True, exist_ok=True)
        (home / "server.json").write_text(json.dumps({"port": 8123, "host": "10.0.0.5"}))
        monkeypatch.setenv("SKEIN_PORT", "8222")
        resolved = resolve_service_config()
        assert resolved.sources["port"] == "SKEIN_PORT"
        assert resolved.sources["host"] == str(home / "server.json")
        assert resolved.config["port"] == 8222
        assert resolved.config["host"] == "10.0.0.5"


class TestMainPortGuard:
    """The service half of the guard: resolution ignores a broken SKEIN_PORT so
    the CLI never crashes on it, but skein-server must not silently bind a port
    the operator did not ask for — it refuses to start instead."""

    def test_main_refuses_an_unparseable_skein_port(
        self, clean_env, no_config_file, monkeypatch
    ):
        from skein import server

        monkeypatch.setenv("SKEIN_PORT", "80o1")
        with pytest.raises(SystemExit) as excinfo:
            server.main([])
        assert "SKEIN_PORT" in str(excinfo.value)

    def test_an_explicit_port_flag_outranks_a_broken_skein_port(
        self, clean_env, no_config_file, monkeypatch
    ):
        from skein import server

        captured = {}
        monkeypatch.setattr("skein.server.uvicorn.run", lambda app, **kw: captured.update(kw))
        monkeypatch.setenv("SKEIN_PORT", "80o1")
        server.main(["--port", "9001"])
        assert captured["port"] == 9001

    def test_port_flag_outranks_a_valid_skein_port(
        self, clean_env, no_config_file, monkeypatch
    ):
        from skein import server

        captured = {}
        monkeypatch.setattr("skein.server.uvicorn.run", lambda app, **kw: captured.update(kw))
        monkeypatch.setenv("SKEIN_PORT", "8123")
        server.main(["--port", "9001"])
        assert captured["port"] == 9001


class TestLauncherShim:
    """The repo-root launcher keeps working for systemd and the fidelity harness."""

    def test_shim_reexports_the_packaged_service(self):
        import skein_server
        from skein.server import app, get_config as packaged_get_config

        assert skein_server.app is app
        assert skein_server.get_config is packaged_get_config
