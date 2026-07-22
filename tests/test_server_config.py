"""Tests for SKEIN service configuration loading.

The service moved into the package (skein.server) so a wheel install can run it;
`skein_server.py` at the repo root is now a launcher that re-exports it. These
tests target the package module and cover the search order a wheel user depends
on: an explicit override, then <SKEIN_HOME>/server.json, then the repo config.
"""

import json
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
    monkeypatch.setattr("skein.server.REPO_CONFIG_PATH", tmp_path / "absent" / "config.json")


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
        monkeypatch.setattr("skein.server.REPO_CONFIG_PATH", config_file)

        config = get_config()
        assert config["host"] == "0.0.0.0"

    def test_env_var_beats_config_file(self, clean_env, no_config_file, monkeypatch, tmp_path):
        """SKEIN_HOST takes precedence over a config.json value."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"server": {"host": "10.0.0.5"}}))
        monkeypatch.setattr("skein.server.REPO_CONFIG_PATH", config_file)
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


class TestLauncherShim:
    """The repo-root launcher keeps working for systemd and the fidelity harness."""

    def test_shim_reexports_the_packaged_service(self):
        import skein_server
        from skein.server import app, get_config as packaged_get_config

        assert skein_server.app is app
        assert skein_server.get_config is packaged_get_config
