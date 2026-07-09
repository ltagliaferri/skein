"""Tests for fidelity/harness.py's registry save/restore.

Closes finding-20260709-opos: the harness once pinned REGISTRY = ~/.skein/projects.json
at import (ignoring SKEIN_HOME) and write_text()'d the LIVE registry non-atomically —
the exact issue-20260709-zl71 incident pattern. These prove it now resolves the home
fresh via skein_home() and routes writes through the atomic storage writers —
save_project_registry() to register the fixture, save_project_registry_text() to
restore the captured pre-image byte-for-byte (which also fixes the teardown crash on
a pre-existing empty registry) — while keeping its save/restore semantics intact.
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


def test_register_preserves_prior_projects(monkeypatch, tmp_path, harness):
    """Registering the fixture leaves prior projects intact, and restore brings the
    registry back to EXACTLY its pre-register bytes — not a reserialized equivalent.
    The pre-image is deliberately non-canonical (compact, keys out of the sorted
    order save_project_registry would emit), so a reserializing restore would change
    the bytes and this assertion would catch it."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    registry = tmp_path / "projects.json"
    before = '{"projects":{"keep-me":{"name":"keep-me","data_dir":"/tmp/k"}}}'
    registry.write_text(before)

    prior = harness.register_fixture()
    reg = json.loads(registry.read_text())["projects"]
    assert set(reg) == {"keep-me", harness.FIX_ID}  # both present after register

    harness.restore_registry(prior)
    assert registry.read_text() == before  # exact bytes back, not a reparse


def test_restore_keeps_empty_file_empty(monkeypatch, tmp_path, harness):
    """A pre-existing EMPTY projects.json (0 bytes) must come back as a 0-byte file
    after restore — not vanish, not crash. The old save_project_registry(json.loads(
    prior)) died here on json.loads(""), leaving the fixture entry stuck; a raw,
    byte-verbatim write neither reparses nor unlinks the empty pre-image."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    registry = tmp_path / "projects.json"
    registry.write_text("")  # exists, but empty

    prior = harness.register_fixture()
    assert prior == ""  # empty text captured, distinct from None (absent)

    harness.restore_registry(prior)  # must not raise on the empty pre-image
    assert registry.exists()          # restored, not unlinked
    assert registry.read_text() == ""  # 0 bytes back, byte-verbatim


def test_restore_absent_file_stays_absent(monkeypatch, tmp_path, harness):
    """restore_registry(None) means 'no file existed before' — it removes whatever
    registry is present, restoring the absent state (idempotent if already absent)."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    registry = tmp_path / "projects.json"
    registry.write_text('{"projects": {}}')

    harness.restore_registry(None)
    assert not registry.exists()  # absent before -> absent after

    harness.restore_registry(None)  # idempotent: no crash when already gone
    assert not registry.exists()
