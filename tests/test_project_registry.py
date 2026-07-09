"""Tests for the SKEIN project-registry resolver and atomic writer.

Guards issue-20260709-zl71: a test run merge-wrote the LIVE ~/.skein/projects.json
and, catching it mid-truncate, destroyed all 50 project registrations. Two fixes,
belt-and-suspenders and deliberately WITHOUT file locking:

  A. SKEIN_HOME redirects the whole ~/.skein tree, so tests never touch the real one.
  B. save_project_registry writes atomically (unique temp + os.replace) after
     snapshotting a rotating backup, so a torn or empty write cannot destroy the
     registry.
"""

import json
import os
from pathlib import Path

import pytest

from skein import storage as storage_mod
from skein.storage import (
    load_project_registry,
    save_project_registry,
    skein_home,
    _prune_registry_backups,
    _REGISTRY_BACKUP_KEEP,
)


def _registry(tag: str) -> dict:
    """A minimal one-project registry payload, tagged so writes are distinguishable."""
    return {"projects": {tag: {"data_dir": f"/tmp/{tag}", "name": tag}}}


def _stamped_backups(home: Path) -> list:
    """Names of the timestamped registry backups under ``home`` (excludes
    manually-named ones like ``.bak-fix``)."""
    return sorted(
        p.name
        for p in home.glob("projects.json.bak-*")
        if storage_mod._REGISTRY_BACKUP_RE.search(p.name)
    )


# --- Fix A: the SKEIN_HOME resolver ---------------------------------------


def test_skein_home_honors_env(monkeypatch, tmp_path):
    """skein_home() returns $SKEIN_HOME when set, ~/.skein otherwise."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    assert skein_home() == tmp_path

    monkeypatch.delenv("SKEIN_HOME", raising=False)
    assert skein_home() == Path.home() / ".skein"


def test_skein_home_read_fresh_each_call(monkeypatch, tmp_path):
    """A mid-process env change is honored immediately — a server subprocess (or a
    test) that sets SKEIN_HOME after import must not be shadowed by a cached value."""
    monkeypatch.delenv("SKEIN_HOME", raising=False)
    assert skein_home() == Path.home() / ".skein"
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    assert skein_home() == tmp_path


def test_load_project_registry_reads_from_skein_home(monkeypatch, tmp_path):
    """load_project_registry() reads <SKEIN_HOME>/projects.json, not ~/.skein."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    (tmp_path / "projects.json").write_text(
        json.dumps({"projects": {"proj-x": {"data_dir": "/tmp/x", "name": "proj-x"}}})
    )
    reg = load_project_registry()
    assert set(reg) == {"proj-x"}
    assert reg["proj-x"]["data_dir"] == "/tmp/x"


def test_load_project_registry_missing_returns_empty(monkeypatch, tmp_path):
    """No projects.json under SKEIN_HOME -> {} (never an error, never a silent
    fallback to the real ~/.skein)."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    assert load_project_registry() == {}


# --- Fix B: atomic writer + rotating backups ------------------------------


def test_save_writes_readable_registry(monkeypatch, tmp_path):
    """A saved registry round-trips through the reader."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    save_project_registry(_registry("alpha"))
    reg_file = tmp_path / "projects.json"
    assert reg_file.exists()
    assert json.loads(reg_file.read_text())["projects"]["alpha"]["name"] == "alpha"
    assert set(load_project_registry()) == {"alpha"}


def test_save_creates_home_and_no_backup_on_first_write(monkeypatch, tmp_path):
    """First write (nothing to snapshot) creates the home dir + file and NO backup."""
    home = tmp_path / "fresh"
    monkeypatch.setenv("SKEIN_HOME", str(home))
    save_project_registry(_registry("alpha"))
    assert (home / "projects.json").exists()
    assert _stamped_backups(home) == []


def test_save_backs_up_previous_on_overwrite(monkeypatch, tmp_path):
    """Overwriting snapshots the PRIOR content to a timestamped backup."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    save_project_registry(_registry("v1"))
    save_project_registry(_registry("v2"))

    live = json.loads((tmp_path / "projects.json").read_text())
    assert live["projects"] == _registry("v2")["projects"]

    backups = _stamped_backups(tmp_path)
    assert len(backups) == 1
    snap = json.loads((tmp_path / backups[0]).read_text())
    assert snap["projects"] == _registry("v1")["projects"]


def test_save_same_second_double_save_distinct_backups(monkeypatch, tmp_path):
    """Two saves within the SAME UTC second must snapshot to two DISTINCT backups,
    not collide on one name (which would let shutil.copy2 overwrite the good
    pre-image). Freeze the clock to one second with incrementing microseconds so the
    only thing distinguishing the two stamps is the %f field."""
    from datetime import datetime as _dt, timezone as _tz

    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    save_project_registry(_registry("v1"))  # first write: creates the file, no backup

    base = _dt(2026, 7, 9, 12, 0, 0, tzinfo=_tz.utc)
    micros = iter([100000, 200000])  # same second, two different microsecond values

    class _FrozenClock:
        @staticmethod
        def now(tz=None):
            return base.replace(microsecond=next(micros))

    monkeypatch.setattr(storage_mod, "datetime", _FrozenClock)

    save_project_registry(_registry("v2"))  # snapshots v1 at ...120000-100000
    save_project_registry(_registry("v3"))  # snapshots v2 at ...120000-200000

    backups = _stamped_backups(tmp_path)
    assert len(backups) == 2  # distinct names, neither overwrote the other
    assert backups == [
        "projects.json.bak-20260709-120000-100000",
        "projects.json.bak-20260709-120000-200000",
    ]
    # The first backup still holds v1 — the second save did NOT destroy it.
    v1_snap = json.loads((tmp_path / backups[0]).read_text())
    assert v1_snap["projects"] == _registry("v1")["projects"]


def test_save_atomic_never_empties_on_failed_replace(monkeypatch, tmp_path):
    """If os.replace fails mid-write, projects.json keeps the OLD complete content
    (never empty, never truncated) and no temp file is left behind — the
    'either old or new, never empty' invariant."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    save_project_registry(_registry("good"))

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(storage_mod.os, "replace", boom)
    with pytest.raises(OSError):
        save_project_registry(_registry("doomed"))

    data = json.loads((tmp_path / "projects.json").read_text())
    assert data["projects"] == _registry("good")["projects"]  # intact, non-empty
    # No stray temp files (mkstemp names are dotfiles, so scan every entry).
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_save_prunes_to_newest_five_and_spares_named(monkeypatch, tmp_path):
    """save() prunes timestamped backups to the newest 5 and never touches
    manually-named backups (.bak-fix, .bak-pre-gnomon)."""
    monkeypatch.setenv("SKEIN_HOME", str(tmp_path))
    (tmp_path / "projects.json").write_text(json.dumps(_registry("live")))

    # Six timestamped backups with distinct PAST stamps (name sorts chronologically;
    # microsecond field varies so all six are the current full format).
    for n in range(1, 7):
        (tmp_path / f"projects.json.bak-20200101-000000-0000{n:02d}").write_text("{}")
    # Two manually-named backups that must survive.
    (tmp_path / "projects.json.bak-fix").write_text("keep me")
    (tmp_path / "projects.json.bak-pre-gnomon").write_text("keep me too")

    # One save -> creates a 7th (current, 2026) timestamped backup, prunes to 5.
    save_project_registry(_registry("live2"))

    remaining = _stamped_backups(tmp_path)
    assert len(remaining) == _REGISTRY_BACKUP_KEEP
    # The two oldest seeded stamps were pruned; a newer seeded stamp survives.
    assert "projects.json.bak-20200101-000000-000001" not in remaining
    assert "projects.json.bak-20200101-000000-000002" not in remaining
    assert "projects.json.bak-20200101-000000-000006" in remaining
    # Manually-named backups untouched.
    assert (tmp_path / "projects.json.bak-fix").read_text() == "keep me"
    assert (tmp_path / "projects.json.bak-pre-gnomon").read_text() == "keep me too"


def test_prune_helper_keeps_newest_five(tmp_path):
    """The prune helper in isolation: newest 5 timestamped kept, older dropped,
    non-timestamped spared."""
    reg_file = tmp_path / "projects.json"
    reg_file.write_text("{}")
    # Seven distinct stamps within one second, differing only in microseconds — the
    # fixed-width %f field keeps name order == time order.
    for n in range(1, 8):
        (tmp_path / f"projects.json.bak-20260701-120000-00000{n}").write_text(str(n))
    (tmp_path / "projects.json.bak-fix").write_text("manual")

    _prune_registry_backups(reg_file)

    remaining = _stamped_backups(tmp_path)
    assert len(remaining) == _REGISTRY_BACKUP_KEEP
    assert "projects.json.bak-20260701-120000-000001" not in remaining  # oldest pruned
    assert "projects.json.bak-20260701-120000-000002" not in remaining
    assert "projects.json.bak-20260701-120000-000007" in remaining  # newest kept
    assert (tmp_path / "projects.json.bak-fix").exists()  # manual spared


# --- Fix A: proof that pytest_configure is isolated -----------------------


def test_pytest_configure_isolated_from_real_home():
    """pytest_configure must have redirected SKEIN_HOME to a throwaway dir and
    seeded the registry THERE — never in the real ~/.skein. Proven via the env
    indirection: the resolver points at the sandbox, not real home, so configure
    could not have read or written the real projects.json."""
    assert "SKEIN_HOME" in os.environ
    sandbox = Path(os.environ["SKEIN_HOME"])
    real_home = Path.home() / ".skein"
    assert sandbox != real_home  # redirected away from the real home
    assert skein_home() == sandbox  # the resolver follows the redirect

    # configure seeded test-project in the sandbox, and every registry read
    # (load_project_registry) resolves it there — not in ~/.skein.
    seeded = json.loads((sandbox / "projects.json").read_text())
    assert "test-project" in seeded["projects"]
    assert "test-project" in load_project_registry()
