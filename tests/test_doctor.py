"""Tests for `skein doctor`.

`doctor` is the answer to "is this install working?" for someone with no source
checkout, so its job is to name what is wrong rather than to be reassuring. Two
of these exist because it was reassuring when it should not have been: it
reported "no projects registered yet" while the operator's registry had just
been deleted out from under 55 projects, and it reported the packaged docs fine
when the installed file was a 31-byte path string.

Every test redirects SKEIN_HOME into tmp_path, so none can read or write a real
``~/.skein``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from client.cli import doctor_checks

REPO_ROOT = Path(__file__).resolve().parent.parent
UNREACHABLE = "http://127.0.0.1:1"


@pytest.fixture
def skein_home(tmp_path, monkeypatch):
    home = tmp_path / "skein-home"
    home.mkdir()
    monkeypatch.setenv("SKEIN_HOME", str(home))
    return home


@pytest.fixture
def outside_a_project(monkeypatch, tmp_path):
    """Run the checks from a directory with no .skein/ above it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return elsewhere


def checks_by_name(base_url=UNREACHABLE):
    return {c["name"]: c for c in doctor_checks(base_url)}


class TestRegistryDamage:
    """An absent registry means one of two very different things."""

    def test_absent_registry_outside_a_project_is_benign(self, skein_home, outside_a_project):
        check = checks_by_name()["project registry"]
        assert check["ok"] is True
        assert check["level"] == "info"

    def test_absent_registry_inside_a_project_is_an_error(self, skein_home, tmp_path, monkeypatch):
        """The shape of a destroyed registry: an initialized project the
        registry has never heard of. This must not read as a new install."""
        project = tmp_path / "myproject"
        (project / ".skein").mkdir(parents=True)
        (project / ".skein" / "config.json").write_text(
            json.dumps({"project_id": "myproject", "name": "myproject"})
        )
        monkeypatch.chdir(project)

        check = checks_by_name()["project registry"]

        assert check["ok"] is False
        assert check["level"] == "error"
        assert "myproject" in check["detail"]
        assert "backup" in check["hint"]

    def test_stale_data_dir_is_a_warning(self, skein_home, tmp_path, outside_a_project):
        (skein_home / "projects.json").write_text(
            json.dumps({"projects": {"gone": {"data_dir": str(tmp_path / "nope")}}})
        )
        check = checks_by_name()["project registry"]
        assert check["ok"] is False
        assert check["level"] == "warn"
        assert "gone" in check["detail"]

    def test_unreadable_registry_is_an_error(self, skein_home, outside_a_project):
        (skein_home / "projects.json").write_text("{ not json")
        check = checks_by_name()["project registry"]
        assert check["ok"] is False
        assert check["level"] == "error"


class TestService:
    def test_absent_service_is_an_error_that_says_how_to_start_one(
        self, skein_home, outside_a_project
    ):
        check = checks_by_name()["api service"]
        assert check["ok"] is False
        assert check["level"] == "error"
        # The hint must name the packaged entrypoint, since there is no
        # `skein` command that starts a service.
        assert "skein-server" in check["hint"]

    def test_version_check_is_skipped_when_nothing_is_running(self, skein_home, outside_a_project):
        check = checks_by_name()["version match"]
        assert check["ok"] is True
        assert check["level"] == "info"


class TestVersionSkew:
    def _with_health(self, monkeypatch, body):
        class FakeResponse:
            status_code = 200

            def json(self):
                return body

        monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse())

    def test_mismatched_versions_are_an_error(self, skein_home, outside_a_project, monkeypatch):
        self._with_health(monkeypatch, {"status": "healthy", "version": "0.0.1"})
        check = checks_by_name()["version match"]
        assert check["ok"] is False
        assert check["level"] == "error"
        assert "0.0.1" in check["detail"]

    def test_a_service_predating_version_reporting_is_a_warning(
        self, skein_home, outside_a_project, monkeypatch
    ):
        self._with_health(monkeypatch, {"status": "healthy"})
        check = checks_by_name()["version match"]
        assert check["ok"] is False
        assert check["level"] == "warn"

    def test_matching_versions_pass(self, skein_home, outside_a_project, monkeypatch):
        from skein.version import package_version

        self._with_health(monkeypatch, {"status": "healthy", "version": package_version()})
        assert checks_by_name()["version match"]["ok"] is True

    def test_metadata_disagreeing_with_imported_source_is_reported(
        self, skein_home, outside_a_project, monkeypatch
    ):
        """Distribution metadata can describe a different install than the code
        that got imported. Reporting only the metadata would make both ends of
        the skew comparison agree on the same wrong number."""
        monkeypatch.setattr("skein.version.package_version", lambda: "9.9.9")
        check = checks_by_name()["install"]
        assert check["ok"] is False
        assert check["level"] == "warn"
        assert "9.9.9" in check["detail"]


class TestPackagedDocs:
    def test_docs_present_in_this_install(self, skein_home, outside_a_project):
        assert checks_by_name()["packaged docs"]["ok"] is True

    def test_a_link_target_masquerading_as_a_document_is_caught(
        self, skein_home, outside_a_project, monkeypatch, tmp_path
    ):
        """What a wheel built from a symlink-less checkout actually installs."""
        placeholder = tmp_path / "SKEIN_QUICK_START.md"
        placeholder.write_text("../../docs/SKEIN_QUICK_START.md")
        monkeypatch.setattr("client.cli.resolve_doc", lambda topic: placeholder)

        check = checks_by_name()["packaged docs"]

        assert check["ok"] is False
        assert "not a document" in check["detail"]


def test_exit_code_and_json_shape(skein_home, tmp_path):
    """Exit 1 on a failing check; the JSON shape is what a script reads."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "client.cli", "doctor", "--json"],
        capture_output=True,
        text=True,
        cwd=str(elsewhere),
        # PYTHONPATH pins the checkout: run from outside the repo, `python -m
        # client.cli` would otherwise import whatever interskein is installed on
        # this machine and test that instead of the code under test.
        env={
            **os.environ,
            "SKEIN_HOME": str(skein_home),
            "SKEIN_URL": UNREACHABLE,
            "PYTHONPATH": str(REPO_ROOT),
        },
        timeout=120,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["healthy"] is False
    assert payload["server_url"] == UNREACHABLE
    names = [c["name"] for c in payload["checks"]]
    assert "api service" in names and "version match" in names


def test_doctor_runs_without_a_source_checkout(skein_home, tmp_path):
    """It is the command a wheel user runs when nothing works, so it must not
    depend on being run from the repo."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "client.cli", "doctor"],
        capture_output=True,
        text=True,
        cwd=str(elsewhere),
        env={
            **os.environ,
            "SKEIN_HOME": str(skein_home),
            "SKEIN_URL": UNREACHABLE,
            "PYTHONPATH": str(REPO_ROOT),
        },
        timeout=120,
    )
    assert "api service" in result.stdout
    assert "skein-server" in result.stdout
