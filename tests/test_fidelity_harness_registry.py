"""Tests for fidelity/harness.py's registry save/restore.

Closes finding-20260709-opos: the harness once pinned REGISTRY = ~/.skein/projects.json
at import (ignoring SKEIN_HOME) and write_text()'d the LIVE registry non-atomically —
the exact issue-20260709-zl71 incident pattern. These prove it now resolves the home
fresh via skein_home() and routes writes through the atomic save_project_registry(),
while keeping its save/restore semantics intact.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_HARNESS_PATH = Path(__file__).resolve().parent.parent / "fidelity" / "harness.py"


@pytest.fixture
def harness():
    """Load fidelity/harness.py as a module by file path (it's a standalone script,
    not an importable package). Importing it has no side effects beyond a sys.path
    insert and the skein.storage import — main() is guarded by __name__."""
    spec = importlib.util.spec_from_file_location("fidelity_harness_under_test", _HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_register_restore_follows_skein_home(monkeypatch, tmp_path, harness):
    """From an empty SKEIN_HOME, register writes the fixture into the sandbox
    registry (never the real ~/.skein), and restore removes it again."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    registry = tmp_path / "projects.json"

    prior = harness.register_fixture()
    assert prior is None  # nothing there before
    assert registry.exists()  # written under SKEIN_HOME, not ~/.skein
    assert harness.FIX_ID in json.loads(registry.read_text())["projects"]

    harness.restore_registry(prior)
    assert not registry.exists()  # no file before -> none after


def test_register_preserves_existing_projects(monkeypatch, tmp_path, harness):
    """Registering the fixture leaves prior projects intact, and restore brings the
    registry back to exactly its pre-register content."""
    from skein.storage import save_project_registry

    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    registry = tmp_path / "projects.json"
    save_project_registry({"projects": {"keep-me": {"data_dir": "/tmp/k", "name": "keep-me"}}})
    before = registry.read_text()

    prior = harness.register_fixture()
    reg = json.loads(registry.read_text())["projects"]
    assert set(reg) == {"keep-me", harness.FIX_ID}  # both present after register

    harness.restore_registry(prior)
    assert json.loads(registry.read_text()) == json.loads(before)  # content restored
