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

# Every test that seals a path with chmod 0 needs real mode-bit enforcement;
# root passes any permission check, so the sealed state cannot be built.
needs_mode_bits = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores mode bits, so nothing can be made unsearchable"
)


@pytest.fixture
def skein_home(tmp_path, monkeypatch):
    home = tmp_path / "skein-home"
    home.mkdir()
    monkeypatch.setenv("SKEIN_HOME", str(home))
    return home


@pytest.fixture
def sealed_home(tmp_path, monkeypatch):
    """A SKEIN_HOME that exists but denies search — what a stray `sudo skein`
    leaves behind as a root-owned ~/.skein. Every stat through it raises
    PermissionError instead of answering."""
    home = tmp_path / "sealed-home"
    home.mkdir()
    home.chmod(0)
    monkeypatch.setenv("SKEIN_HOME", str(home))
    yield home
    home.chmod(0o700)  # so tmp_path cleanup can remove it


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

    def test_absent_registry_with_a_backup_is_damage_even_outside_a_project(
        self, skein_home, outside_a_project
    ):
        """Backups exist only after the registry was written at least once. So a
        backup present with no live file is a deletion, wherever doctor runs — the
        exact situation the check was added for, missed when outside a project."""
        (skein_home / "projects.json.bak-20260101-000000-000000").write_text(
            json.dumps({"projects": {"real": {"data_dir": "/x"}}})
        )
        check = checks_by_name()["project registry"]
        assert check["ok"] is False
        assert check["level"] == "error"
        assert "backup" in check["detail"]

    def test_projects_as_a_list_does_not_crash_and_is_an_error(self, skein_home, outside_a_project):
        """A malformed registry must fail the check, not raise out of doctor."""
        (skein_home / "projects.json").write_text(json.dumps({"projects": []}))
        check = checks_by_name()["project registry"]
        assert check["ok"] is False
        assert check["level"] == "error"

    def test_entry_without_a_data_dir_is_not_a_clean_pass(self, skein_home, outside_a_project):
        """An empty/absent data_dir is Path('') which tests as the cwd — that must
        not pass a broken entry as a registered project."""
        (skein_home / "projects.json").write_text(json.dumps({"projects": {"broken": {}}}))
        check = checks_by_name()["project registry"]
        assert check["ok"] is False
        assert "broken" in check["detail"]

    def test_a_non_string_data_dir_does_not_crash(self, skein_home, outside_a_project):
        """Path(7) raises; doctor must report the entry as broken, not traceback."""
        (skein_home / "projects.json").write_text(
            json.dumps({"projects": {"broken": {"data_dir": 7}}})
        )
        check = checks_by_name()["project registry"]
        assert check["ok"] is False
        assert "broken" in check["detail"]

    @needs_mode_bits
    def test_a_data_dir_behind_an_unsearchable_directory_is_flagged_not_a_crash(
        self, skein_home, tmp_path, outside_a_project
    ):
        """exists() on a path under a directory that denies search raises rather
        than returning False; the entry is as unusable as a missing one."""
        sealed = tmp_path / "sealed-parent"
        sealed.mkdir()
        (skein_home / "projects.json").write_text(
            json.dumps({"projects": {"walled": {"data_dir": str(sealed / "data")}}})
        )
        sealed.chmod(0)
        try:
            check = checks_by_name()["project registry"]
        finally:
            sealed.chmod(0o700)
        assert check["ok"] is False
        assert "walled" in check["detail"]


@needs_mode_bits
class TestUnsearchableHome:
    """SKEIN_HOME exists but cannot be searched — the first-contact state doctor
    exists for. Every probe into it must come back as a failing check with an
    honest exit code, never a traceback, in text and --json alike."""

    def _run_doctor(self, sealed_home, tmp_path, *flags):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        return subprocess.run(
            [sys.executable, "-m", "client.cli", "doctor", *flags],
            capture_output=True,
            text=True,
            cwd=str(elsewhere),
            env={
                **os.environ,
                "SKEIN_HOME": str(sealed_home),
                "SKEIN_URL": UNREACHABLE,
                "PYTHONPATH": str(REPO_ROOT),
            },
            timeout=120,
        )

    def test_probes_become_failing_checks_not_a_crash(self, sealed_home, outside_a_project):
        checks = checks_by_name()
        assert checks["skein home"]["ok"] is False
        registry = checks["project registry"]
        assert registry["ok"] is False
        assert registry["level"] == "error"
        assert str(sealed_home) in registry["detail"]

    def test_text_mode_reports_and_exits_nonzero(self, sealed_home, tmp_path):
        result = self._run_doctor(sealed_home, tmp_path)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Traceback" not in result.stderr
        assert "skein home" in result.stdout
        assert "project registry" in result.stdout
        assert "check(s) failed" in result.stdout

    def test_json_mode_stays_parseable(self, sealed_home, tmp_path):
        result = self._run_doctor(sealed_home, tmp_path, "--json")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Traceback" not in result.stderr
        payload = json.loads(result.stdout)
        assert payload["healthy"] is False
        by_name = {c["name"]: c for c in payload["checks"]}
        assert by_name["skein home"]["ok"] is False
        assert by_name["project registry"]["ok"] is False


@needs_mode_bits
def test_global_config_survives_an_unsearchable_dot_skein(tmp_path, monkeypatch):
    """`skein doctor` resolves its base URL through here before printing a single
    check; an unprobeable ~/.skein must yield the defaults, not a PermissionError."""
    from client.cli import get_global_config

    fake_home = tmp_path / "fake-home"
    dot_skein = fake_home / ".skein"
    dot_skein.mkdir(parents=True)
    dot_skein.chmod(0)
    monkeypatch.setenv("HOME", str(fake_home))
    try:
        config = get_global_config()
    finally:
        dot_skein.chmod(0o700)
    assert config == {"server_url": "http://localhost:8001"}


@needs_mode_bits
def test_an_unsearchable_project_dot_skein_is_a_failed_check_not_a_crash(
    skein_home, tmp_path, monkeypatch
):
    """A project whose .skein/ denies search: the config probe raises where the
    directory walk succeeded. Doctor must report the project as broken."""
    project = tmp_path / "proj"
    (project / ".skein").mkdir(parents=True)
    (project / ".skein").chmod(0)
    monkeypatch.chdir(project)
    try:
        check = checks_by_name()["current project"]
    finally:
        (project / ".skein").chmod(0o700)
    assert check["ok"] is False


class TestService:
    def _with_health(self, monkeypatch, body):
        class FakeResponse:
            status_code = 200

            def json(self):
                return body

        monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse())

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

    def test_an_unhealthy_200_is_not_a_healthy_service(
        self, skein_home, outside_a_project, monkeypatch
    ):
        """A 200 is not proof: anything can hold a fixed localhost port."""
        self._with_health(monkeypatch, {"status": "unhealthy"})
        check = checks_by_name()["api service"]
        assert check["ok"] is False
        assert "not a healthy SKEIN service" in check["detail"]

    def test_a_foreign_distribution_is_not_a_skein_service(
        self, skein_home, outside_a_project, monkeypatch
    ):
        from skein.version import package_version

        self._with_health(
            monkeypatch,
            {"status": "healthy", "distribution": "not-skein", "version": package_version()},
        )
        check = checks_by_name()["api service"]
        assert check["ok"] is False

    def test_a_service_serving_a_different_home_is_flagged(
        self, skein_home, outside_a_project, monkeypatch
    ):
        """Healthy and same version, but a different SKEIN_HOME: the CLI's projects
        are invisible to it, so `skein sites` would fail. Doctor must not say ok."""
        from skein.version import package_version

        self._with_health(
            monkeypatch,
            {
                "status": "healthy",
                "distribution": "interskein",
                "version": package_version(),
                "skein_home": "/some/other/home",
            },
        )
        check = checks_by_name()["api service"]
        assert check["ok"] is False
        assert "SKEIN_HOME" in check["detail"]

    def test_a_malformed_home_does_not_crash_or_false_fail(
        self, skein_home, outside_a_project, monkeypatch
    ):
        """A non-string skein_home (a broken/impostor body) must not crash doctor;
        it is left uncompared, like a service that omits the field."""
        from skein.version import package_version

        self._with_health(
            monkeypatch,
            {
                "status": "healthy",
                "distribution": "interskein",
                "version": package_version(),
                "skein_home": 42,
            },
        )
        check = checks_by_name()["api service"]
        # Did not raise; treated as uncomparable, so the service check passes and
        # the overall verdict rests on the version check (which matches here).
        assert check["ok"] is True

    def test_a_matching_home_healthy_service_passes(
        self, skein_home, outside_a_project, monkeypatch
    ):
        from skein.version import package_version

        self._with_health(
            monkeypatch,
            {
                "status": "healthy",
                "distribution": "interskein",
                "version": package_version(),
                "skein_home": str(skein_home),
            },
        )
        check = checks_by_name()["api service"]
        assert check["ok"] is True


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

    def test_a_missing_non_quickstart_topic_is_caught(
        self, skein_home, outside_a_project, monkeypatch
    ):
        """The check is plural — every topic `skein info` serves must resolve, not
        just quickstart. A guide/implementation that is gone must fail the check."""
        from client import cli

        real = cli.resolve_doc
        monkeypatch.setattr(
            "client.cli.resolve_doc",
            lambda topic: None if topic != "quickstart" else real("quickstart"),
        )

        check = checks_by_name()["packaged docs"]

        assert check["ok"] is False
        assert "guide" in check["detail"] or "implementation" in check["detail"]

    @needs_mode_bits
    def test_an_unreadable_doc_is_reported_not_a_crash(
        self, skein_home, outside_a_project, monkeypatch, tmp_path
    ):
        """A doc whose mode denies reading (a sudo'd install): stat succeeds,
        read_text raises. That is a failed check, not a traceback."""
        locked = tmp_path / "SKEIN_QUICK_START.md"
        locked.write_text("# real enough\n" + "x" * 600)
        locked.chmod(0)
        monkeypatch.setattr("client.cli.resolve_doc", lambda topic: locked)

        check = checks_by_name()["packaged docs"]

        assert check["ok"] is False
        assert "unreadable" in check["detail"]


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
