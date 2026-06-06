"""Tests for SKEIN server configuration loading."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from skein_server import get_config


@pytest.fixture
def no_config_file(monkeypatch, tmp_path):
    """Point get_config at a directory with no config.json so file lookup misses."""
    fake_module_file = tmp_path / "skein_server.py"
    fake_module_file.write_text("")
    monkeypatch.setattr("skein_server.__file__", str(fake_module_file))


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

    def test_skein_host_env_overrides_to_all_interfaces(
        self, clean_env, no_config_file, monkeypatch
    ):
        """Operators can opt in to network exposure via SKEIN_HOST=0.0.0.0."""
        monkeypatch.setenv("SKEIN_HOST", "0.0.0.0")
        config = get_config()
        assert config["host"] == "0.0.0.0"

    def test_config_file_overrides_default(self, clean_env, monkeypatch, tmp_path):
        """A config.json with a server.host value takes precedence over the default."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"server": {"host": "0.0.0.0"}})
        )
        fake_module_file = tmp_path / "skein_server.py"
        fake_module_file.write_text("")
        monkeypatch.setattr("skein_server.__file__", str(fake_module_file))

        config = get_config()
        assert config["host"] == "0.0.0.0"

    def test_env_var_beats_config_file(self, clean_env, monkeypatch, tmp_path):
        """SKEIN_HOST takes precedence over a config.json value."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"server": {"host": "10.0.0.5"}})
        )
        fake_module_file = tmp_path / "skein_server.py"
        fake_module_file.write_text("")
        monkeypatch.setattr("skein_server.__file__", str(fake_module_file))
        monkeypatch.setenv("SKEIN_HOST", "192.168.1.10")

        config = get_config()
        assert config["host"] == "192.168.1.10"
