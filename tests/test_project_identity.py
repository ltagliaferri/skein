"""Fail-closed project identity routing.

These tests guard issue-20260725-b3a5.  A copied project can carry the same
``.skein/config.json`` as its source, while the service still maps that
``project_id`` to the source project's data directory.  An implicit command
from the copy must not silently operate on the source.
"""

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from client.cli import cli, doctor_checks, make_request
from skein.models import Folio, Site
from skein.routes import router
from skein.storage import JSONStore, project_data_dirs_match, save_project_registry
from skein.storage import (
    ProjectRegistryError,
    decode_project_data_dir_claim,
    encode_project_data_dir_claim,
    register_project,
)


def _write_project(project_root: Path, project_id: str) -> Path:
    data_dir = project_root / ".skein" / "data"
    data_dir.mkdir(parents=True)
    (project_root / ".skein" / "config.json").write_text(
        json.dumps({"project_id": project_id, "name": project_id})
    )
    return data_dir


def _write_registry(home: Path, project_id: str, project_root: Path) -> bytes:
    data_dir = project_root / ".skein" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "projects": {
            project_id: {
                "path": str(project_root),
                "data_dir": str(data_dir),
                "name": project_id,
            }
        }
    }
    raw = (json.dumps(payload, indent=2) + "\n").encode()
    home.mkdir(parents=True, exist_ok=True)
    (home / "projects.json").write_bytes(raw)
    return raw


def _successful_response() -> MagicMock:
    response = MagicMock()
    response.text = "[]"
    response.json.return_value = []
    response.ok = True
    response.raise_for_status.return_value = None
    return response


class TestClientOriginClaim:
    def test_addressed_folio_requests_cannot_bypass_address_helper(self):
        endpoint = "/folios/" + "canonical:brief-test"

        with pytest.raises(ValueError, match="make_folio_request"):
            make_request("GET", endpoint, "http://service", "agent")

    def test_implicit_cwd_project_claims_its_discovered_data_dir(
        self, tmp_path, monkeypatch
    ):
        project_root = tmp_path / "copy"
        data_dir = _write_project(project_root, "alpha")
        monkeypatch.chdir(project_root)
        monkeypatch.delenv("SKEIN_PROJECT", raising=False)

        with patch("client.cli.requests.request", return_value=_successful_response()) as request:
            make_request("GET", "/sites", "http://service", "agent")

        headers = request.call_args.kwargs["headers"]
        assert headers["X-Project-Id"] == "alpha"
        claim = headers["X-Skein-Project-Data-Dir"]
        assert claim.encode("ascii")
        assert decode_project_data_dir_claim(claim) == data_dir.resolve()

    @pytest.mark.parametrize("name", ["プロジェクト", "bad-\udcff-byte"])
    def test_implicit_origin_claim_is_ascii_safe_and_reversible(
        self, tmp_path, monkeypatch, name
    ):
        project_root = tmp_path / name
        data_dir = _write_project(project_root, "alpha")
        monkeypatch.chdir(project_root)
        monkeypatch.delenv("SKEIN_PROJECT", raising=False)
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                captured["claim"] = self.headers["X-Skein-Project-Data-Dir"]
                body = b"[]"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            make_request(
                "GET",
                "/sites",
                f"http://127.0.0.1:{server.server_port}",
                "agent",
            )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        claim = captured["claim"]
        assert claim.isascii()
        assert decode_project_data_dir_claim(claim) == data_dir.resolve()

    def test_explicit_project_kwarg_does_not_claim_the_cwd(
        self, tmp_path, monkeypatch
    ):
        project_root = tmp_path / "copy"
        _write_project(project_root, "copy-id")
        monkeypatch.chdir(project_root)
        monkeypatch.delenv("SKEIN_PROJECT", raising=False)

        with patch("client.cli.requests.request", return_value=_successful_response()) as request:
            make_request(
                "GET",
                "/sites",
                "http://service",
                "agent",
                project_id="canonical",
            )

        headers = request.call_args.kwargs["headers"]
        assert headers["X-Project-Id"] == "canonical"
        assert "X-Skein-Project-Data-Dir" not in headers

    @pytest.mark.parametrize(
        "command", ["brief", "playbook", "ignite", "export", "edit", "move"]
    )
    def test_project_qualified_folio_commands_omit_cwd_claim(
        self, tmp_path, monkeypatch, command
    ):
        project_root = tmp_path / "copy"
        _write_project(project_root, "copy-id")
        monkeypatch.chdir(project_root)
        monkeypatch.delenv("SKEIN_PROJECT", raising=False)
        payload = {
            "folio_id": "brief-20260725-test",
            "type": "brief",
            "title": "Test",
            "content": "content",
            "created_at": "2026-08-23T00:00:00Z",
            "created_by": "test",
            "success": True,
            "folio": {"title": "Test"},
        }
        response = MagicMock()
        response.text = json.dumps(payload)
        response.json.return_value = payload
        response.ok = True
        response.raise_for_status.return_value = None
        address = "canonical:brief-20260725-test"
        args = {
            "brief": ["brief", "get", address, "--json"],
            "playbook": ["playbook", "get", address, "--json"],
            "ignite": ["ignite", address],
            "export": [
                "export",
                address,
                "--format",
                "json",
                "--output",
                str(tmp_path / "out.json"),
            ],
            "edit": ["edit", address, "--title", "Updated"],
            "move": ["move", address, "destination"],
        }[command]

        with patch("client.cli.requests.request", return_value=response) as request:
            result = CliRunner().invoke(
                cli, ["--url", "http://service", "--agent", "agent", *args]
            )

        assert result.exit_code == 0, result.output
        headers = request.call_args_list[0].kwargs["headers"]
        assert headers["X-Project-Id"] == "copy-id"
        assert "X-Skein-Project-Data-Dir" not in headers

    def test_skein_project_override_does_not_claim_the_cwd(
        self, tmp_path, monkeypatch
    ):
        project_root = tmp_path / "copy"
        _write_project(project_root, "copy-id")
        monkeypatch.chdir(project_root)
        monkeypatch.setenv("SKEIN_PROJECT", "canonical")

        with patch("client.cli.requests.request", return_value=_successful_response()) as request:
            make_request("GET", "/sites", "http://service", "agent")

        headers = request.call_args.kwargs["headers"]
        assert headers["X-Project-Id"] == "canonical"
        assert "X-Skein-Project-Data-Dir" not in headers

    def test_project_qualified_folio_read_overrides_a_copied_cwd(
        self, tmp_path, monkeypatch
    ):
        project_root = tmp_path / "copy"
        _write_project(project_root, "copy-id")
        monkeypatch.chdir(project_root)
        monkeypatch.delenv("SKEIN_PROJECT", raising=False)

        with patch(
            "client.cli.requests.request", return_value=_successful_response()
        ) as request:
            result = CliRunner().invoke(
                cli,
                [
                    "--url",
                    "http://service",
                    "--agent",
                    "agent",
                    "folio",
                    "canonical:brief-20260725-test",
                    "--json",
                ],
            )

        assert result.exit_code == 0, result.output
        headers = request.call_args.kwargs["headers"]
        assert headers["X-Project-Id"] == "copy-id"
        assert "X-Skein-Project-Data-Dir" not in headers

    def test_qualified_folio_outside_a_project_uses_target_as_fallback(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SKEIN_PROJECT", raising=False)

        with patch(
            "client.cli.requests.request", return_value=_successful_response()
        ) as request:
            result = CliRunner().invoke(
                cli,
                [
                    "--url",
                    "http://service",
                    "--agent",
                    "agent",
                    "folio",
                    "canonical:brief-20260725-test",
                    "--json",
                ],
            )

        assert result.exit_code == 0, result.output
        headers = request.call_args.kwargs["headers"]
        assert headers["X-Project-Id"] == "canonical"
        assert "X-Skein-Project-Data-Dir" not in headers

    def test_qualified_ignite_outside_a_project_uses_target_as_fallback(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SKEIN_PROJECT", raising=False)
        response = MagicMock()
        response.text = json.dumps({"type": "brief", "content": "mission"})
        response.json.return_value = {"type": "brief", "content": "mission"}
        response.ok = True
        response.raise_for_status.return_value = None

        with patch("client.cli.requests.request", return_value=response) as request:
            result = CliRunner().invoke(
                cli,
                [
                    "--url",
                    "http://service",
                    "--agent",
                    "agent",
                    "ignite",
                    "canonical:brief-20260725-test",
                ],
            )

        assert result.exit_code == 0, result.output
        headers = request.call_args_list[0].kwargs["headers"]
        assert headers["X-Project-Id"] == "canonical"
        assert "X-Skein-Project-Data-Dir" not in headers


class TestInitOwnership:
    def test_existing_project_id_refuses_before_creating_local_state(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        canonical = tmp_path / "canonical"
        before = _write_registry(home, "alpha", canonical)
        copied_root = tmp_path / "copy"
        copied_root.mkdir()
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(copied_root)

        result = CliRunner().invoke(cli, ["init", "--project", "alpha"])

        assert result.exit_code != 0
        assert "already registered" in result.output
        assert str(canonical) in result.output
        assert not (copied_root / ".skein").exists()
        assert (home / "projects.json").read_bytes() == before
        assert list(home.glob("projects.json.bak-*")) == []

    def test_same_owner_can_repair_missing_local_skein(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        project_root = tmp_path / "project"
        before = _write_registry(home, "alpha", project_root)
        # _write_registry creates the data dir; remove only the project-local
        # tree to model doctor finding a stale registry owner.
        import shutil

        shutil.rmtree(project_root / ".skein")
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(project_root)

        result = CliRunner().invoke(cli, ["init", "--project", "alpha"])

        assert result.exit_code == 0, result.output
        config = json.loads((project_root / ".skein" / "config.json").read_text())
        assert config["project_id"] == "alpha"
        repaired = json.loads((home / "projects.json").read_text())
        assert repaired["projects"]["alpha"]["path"] == str(project_root)
        assert before != (home / "projects.json").read_bytes()

    def test_malformed_registry_refuses_before_creating_local_state(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        registry = home / "projects.json"
        before = b"{ definitely not json\n"
        registry.write_bytes(before)
        project_root = tmp_path / "fresh"
        project_root.mkdir()
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(project_root)

        result = CliRunner().invoke(cli, ["init", "--project", "alpha"])

        assert result.exit_code != 0
        assert "project registry" in result.output.lower()
        assert not (project_root / ".skein").exists()
        assert registry.read_bytes() == before
        assert list(home.glob("projects.json.bak-*")) == []

    def test_wrong_registry_shape_refuses_before_creating_local_state(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        registry = home / "projects.json"
        before = b'{"projects": []}\n'
        registry.write_bytes(before)
        project_root = tmp_path / "fresh"
        project_root.mkdir()
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(project_root)

        result = CliRunner().invoke(cli, ["init", "--project", "alpha"])

        assert result.exit_code != 0
        assert "project registry" in result.output.lower()
        assert not (project_root / ".skein").exists()
        assert registry.read_bytes() == before
        assert list(home.glob("projects.json.bak-*")) == []

    def test_relative_existing_owner_refuses_without_rewriting_registry(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        registry = home / "projects.json"
        before = b'{"projects":{"alpha":{"path":".","data_dir":".skein/data"}}}\n'
        registry.write_bytes(before)
        project_root = tmp_path / "project"
        project_root.mkdir()
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(project_root)

        result = CliRunner().invoke(cli, ["init", "--project", "alpha"])

        assert result.exit_code != 0
        assert "non-absolute" in result.output
        assert not (project_root / ".skein").exists()
        assert registry.read_bytes() == before

    def test_locked_registration_refuses_relative_existing_owner(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        registry = home / "projects.json"
        before = b'{"projects":{"alpha":{"path":".","data_dir":".skein/data"}}}\n'
        registry.write_bytes(before)
        candidate = tmp_path / "project"
        monkeypatch.setenv("SKEIN_HOME", str(home))

        with pytest.raises(ProjectRegistryError, match="non-absolute"):
            register_project(
                "alpha",
                {
                    "path": str(candidate),
                    "data_dir": str(candidate / ".skein" / "data"),
                    "name": "alpha",
                },
                allow_same_data_dir=True,
            )

        assert registry.read_bytes() == before

    def test_late_competing_owner_wins_and_local_state_is_rolled_back(
        self, tmp_path, monkeypatch
    ):
        from client import cli as cli_mod

        home = tmp_path / "home"
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(first)
        real_register = cli_mod.register_project

        def race_then_register(project_id, project_info, **kwargs):
            save_project_registry(
                {
                    "projects": {
                        project_id: {
                            "path": str(second),
                            "data_dir": str(second / ".skein" / "data"),
                            "name": project_id,
                        }
                    }
                }
            )
            return real_register(project_id, project_info, **kwargs)

        monkeypatch.setattr(cli_mod, "register_project", race_then_register)

        result = CliRunner().invoke(cli, ["init", "--project", "alpha"])

        assert result.exit_code != 0
        assert "already registered" in result.output
        assert str(second) in result.output
        assert not (first / ".skein").exists()
        registry = json.loads((home / "projects.json").read_text())
        assert registry["projects"]["alpha"]["path"] == str(second)

    def test_registry_save_failure_rolls_back_new_local_state(
        self, tmp_path, monkeypatch
    ):
        from client import cli as cli_mod

        home = tmp_path / "home"
        project_root = tmp_path / "project"
        project_root.mkdir()
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(project_root)

        def fail_registration(*args, **kwargs):
            raise OSError("simulated registry save failure")

        monkeypatch.setattr(cli_mod, "register_project", fail_registration)

        result = CliRunner().invoke(cli, ["init", "--project", "alpha"])

        assert result.exit_code != 0
        assert "simulated registry save failure" in result.output
        assert not (project_root / ".skein").exists()


class TestServiceOriginValidation:
    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_mismatched_implicit_origin_is_refused_before_store_open(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        canonical = tmp_path / "canonical"
        canonical_data = canonical / ".skein" / "data"
        _write_registry(home, "alpha", canonical)
        copied_data = _write_project(tmp_path / "copy", "alpha")
        monkeypatch.setenv("SKEIN_HOME", str(home))
        before = sorted(canonical_data.iterdir())

        response = self._client().get(
            "/sites",
            headers={
                "X-Project-Id": "alpha",
                "X-Skein-Project-Data-Dir": str(copied_data),
            },
        )

        assert response.status_code == 409
        assert "alpha" in response.json()["detail"]
        assert str(canonical_data) in response.json()["detail"]
        assert str(copied_data) in response.json()["detail"]
        assert sorted(canonical_data.iterdir()) == before

    def test_matching_implicit_origin_is_accepted(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        canonical = tmp_path / "canonical"
        canonical_data = canonical / ".skein" / "data"
        _write_registry(home, "alpha", canonical)
        monkeypatch.setenv("SKEIN_HOME", str(home))

        response = self._client().get(
            "/sites",
            headers={
                "X-Project-Id": "alpha",
                "X-Skein-Project-Data-Dir": str(canonical_data),
            },
        )

        assert response.status_code == 200
        assert response.json() == []

    def test_encoded_unicode_origin_is_accepted(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        canonical = tmp_path / "プロジェクト"
        canonical_data = canonical / ".skein" / "data"
        _write_registry(home, "alpha", canonical)
        monkeypatch.setenv("SKEIN_HOME", str(home))

        response = self._client().get(
            "/sites",
            headers={
                "X-Project-Id": "alpha",
                "X-Skein-Project-Data-Dir": encode_project_data_dir_claim(
                    canonical_data
                ),
            },
        )

        assert response.status_code == 200
        assert response.json() == []

    def test_malformed_encoded_origin_is_refused(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        canonical = tmp_path / "canonical"
        _write_registry(home, "alpha", canonical)
        monkeypatch.setenv("SKEIN_HOME", str(home))

        response = self._client().get(
            "/sites",
            headers={
                "X-Project-Id": "alpha",
                "X-Skein-Project-Data-Dir": "b64:not-valid!",
            },
        )

        assert response.status_code == 400
        assert "invalid base64" in response.json()["detail"]

    def test_symlink_alias_to_registered_data_dir_is_accepted(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        canonical = tmp_path / "canonical"
        canonical_data = canonical / ".skein" / "data"
        _write_registry(home, "alpha", canonical)
        alias = tmp_path / "data-alias"
        alias.symlink_to(canonical_data, target_is_directory=True)
        monkeypatch.setenv("SKEIN_HOME", str(home))

        response = self._client().get(
            "/sites",
            headers={
                "X-Project-Id": "alpha",
                "X-Skein-Project-Data-Dir": str(alias),
            },
        )

        assert response.status_code == 200

    def test_unprobeable_existing_data_dir_fails_closed(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        real_stat = Path.stat

        def deny_stat(path, *args, **kwargs):
            if path == data_dir:
                raise PermissionError("simulated unsearchable path")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", deny_stat)

        assert project_data_dirs_match(data_dir, data_dir) is False

    def test_relative_origin_claim_is_refused_without_opening_store(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        canonical = tmp_path / "canonical"
        canonical_data = canonical / ".skein" / "data"
        _write_registry(home, "alpha", canonical)
        monkeypatch.setenv("SKEIN_HOME", str(home))
        before = sorted(canonical_data.iterdir())

        response = self._client().get(
            "/sites",
            headers={
                "X-Project-Id": "alpha",
                "X-Skein-Project-Data-Dir": "../canonical/.skein/data",
            },
        )

        assert response.status_code in {400, 409}
        assert sorted(canonical_data.iterdir()) == before

    def test_log_store_also_refuses_a_mismatched_origin(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        canonical = tmp_path / "canonical"
        canonical_data = canonical / ".skein" / "data"
        _write_registry(home, "alpha", canonical)
        copied_data = _write_project(tmp_path / "copy", "alpha")
        monkeypatch.setenv("SKEIN_HOME", str(home))

        response = self._client().get(
            "/logs/streams",
            headers={
                "X-Project-Id": "alpha",
                "X-Skein-Project-Data-Dir": str(copied_data),
            },
        )

        assert response.status_code == 409
        assert not (canonical_data / "skein.db").exists()

    def test_api_client_without_origin_claim_remains_explicitly_supported(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        canonical = tmp_path / "canonical"
        _write_registry(home, "alpha", canonical)
        monkeypatch.setenv("SKEIN_HOME", str(home))

        response = self._client().get(
            "/sites",
            headers={"X-Project-Id": "alpha"},
        )

        assert response.status_code == 200
        assert response.json() == []

    def test_project_qualified_read_of_unknown_project_returns_404(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        current = tmp_path / "current"
        _write_project(current, "current")
        _write_registry(home, "current", current)
        monkeypatch.setenv("SKEIN_HOME", str(home))
        response = self._client().get(
            "/folios/missing:brief-missing",
            headers={"X-Project-Id": "current"},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == (
            "Project 'missing' not found in registry"
        )
        assert not (tmp_path / "missing").exists()

    def test_project_qualified_read_preserves_source_project(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        current = tmp_path / "current"
        target = tmp_path / "target"
        _write_project(current, "current")
        target_data = _write_project(target, "target")
        payload = {
            "projects": {
                "current": {
                    "path": str(current),
                    "data_dir": str(current / ".skein" / "data"),
                    "name": "current",
                },
                "target": {
                    "path": str(target),
                    "data_dir": str(target_data),
                    "name": "target",
                },
            }
        }
        home.mkdir()
        (home / "projects.json").write_text(json.dumps(payload))
        monkeypatch.setenv("SKEIN_HOME", str(home))
        target_store = JSONStore(target_data)
        target_store.save_site(
            Site(
                site_id="target-site",
                created_at=datetime.now(timezone.utc),
                created_by="test",
                purpose="target",
            )
        )
        target_store.save_folio(
            Folio(
                folio_id="brief-target",
                type="brief",
                site_id="target-site",
                created_at=datetime.now(timezone.utc),
                created_by="test",
                title="Target",
                content="target content",
            )
        )

        response = self._client().get(
            "/folios/target:brief-target",
            headers={"X-Project-Id": "current"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["source_project"] == "target"

    @pytest.mark.parametrize(
        "entry",
        [
            {"path": "/claimed/owner", "data_dir": ""},
            {"path": "/claimed/owner", "data_dir": "relative/data"},
            [],
        ],
    )
    def test_explicit_api_refuses_malformed_registry_entries_before_store_open(
        self, tmp_path, monkeypatch, entry
    ):
        home = tmp_path / "home"
        home.mkdir()
        (home / "projects.json").write_text(
            json.dumps({"projects": {"alpha": entry}})
        )
        service_cwd = tmp_path / "service-cwd"
        service_cwd.mkdir()
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(service_cwd)

        response = self._client().get(
            "/sites",
            headers={"X-Project-Id": "alpha"},
        )

        assert response.status_code == 503
        assert "registry" in response.json()["detail"].lower()
        assert list(service_cwd.iterdir()) == []

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/folios/target:brief-missing", None),
            ("PATCH", "/folios/target:brief-missing", {"title": "changed"}),
            (
                "POST",
                "/folios/target:brief-missing/move",
                {"dest_site_id": "elsewhere"},
            ),
        ],
    )
    def test_project_qualified_routes_refuse_a_malformed_target_before_store_open(
        self, tmp_path, monkeypatch, method, path, body
    ):
        home = tmp_path / "home"
        current = tmp_path / "current"
        current_data = _write_project(current, "current")
        service_cwd = tmp_path / "service-cwd"
        relative_data = service_cwd / "relative" / "data"
        relative_data.mkdir(parents=True)
        home.mkdir()
        (home / "projects.json").write_text(
            json.dumps(
                {
                    "projects": {
                        "current": {
                            "path": str(current),
                            "data_dir": str(current_data),
                            "name": "current",
                        },
                        "target": {
                            "path": "/claimed/target",
                            "data_dir": "relative/data",
                            "name": "target",
                        },
                    }
                }
            )
        )
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(service_cwd)

        response = self._client().request(
            method,
            path,
            headers={"X-Project-Id": "current"},
            json=body,
        )

        assert response.status_code == 503
        assert "registry" in response.json()["detail"].lower()
        assert list(relative_data.iterdir()) == []

    def test_bare_folio_cascade_skips_a_relative_registry_data_dir(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        current = tmp_path / "current"
        current_data = _write_project(current, "current")
        service_cwd = tmp_path / "service-cwd"
        service_cwd.mkdir()
        relative_data = service_cwd / "relative" / "data"
        relative_data.mkdir(parents=True)
        target_store = JSONStore(relative_data)
        target_store.save_site(
            Site(
                site_id="target-site",
                created_at=datetime.now(timezone.utc),
                created_by="test",
                purpose="malformed target",
            )
        )
        target_store.save_folio(
            Folio(
                folio_id="brief-owned-by-service-cwd",
                type="brief",
                site_id="target-site",
                created_at=datetime.now(timezone.utc),
                created_by="test",
                title="Must not escape the registry",
                content="wrong filesystem location",
                metadata={},
            )
        )
        home.mkdir()
        (home / "projects.json").write_text(
            json.dumps(
                {
                    "projects": {
                        "current": {
                            "path": str(current),
                            "data_dir": str(current_data),
                            "name": "current",
                        },
                        "target": {
                            "path": "/claimed/target",
                            "data_dir": "relative/data",
                            "name": "target",
                        },
                    }
                }
            )
        )
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(service_cwd)

        response = self._client().get(
            "/folios/brief-owned-by-service-cwd",
            headers={"X-Project-Id": "current"},
        )

        assert response.status_code == 404


class TestDoctorOriginValidation:
    def test_doctor_reports_copied_project_identity_mismatch(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        canonical = tmp_path / "canonical"
        canonical_data = canonical / ".skein" / "data"
        _write_registry(home, "alpha", canonical)
        copied_root = tmp_path / "copy"
        copied_data = _write_project(copied_root, "alpha")
        monkeypatch.setenv("SKEIN_HOME", str(home))
        monkeypatch.chdir(copied_root)

        checks = doctor_checks("http://127.0.0.1:1")
        current = [check for check in checks if check["name"] == "current project"]

        assert len(current) == 1
        assert current[0]["ok"] is False
        assert str(canonical_data) in current[0]["detail"]
        assert str(copied_data) in current[0]["detail"]
        assert "copied" in current[0]["hint"].lower()
