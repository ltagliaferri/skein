"""CLI tests for SHARD commands."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent))

from client.cli import cli


class _MockShardModule:
    class ShardError(Exception):
        pass

    def __init__(self):
        self.cleanup_calls = []

    def cleanup_shard(self, worktree_name, keep_branch=False, caller_cwd=None):
        self.cleanup_calls.append(
            {
                "worktree_name": worktree_name,
                "keep_branch": keep_branch,
                "caller_cwd": caller_cwd,
            }
        )

    def is_graft(self, worktree_name):
        return False


class TestShardCleanupCli:
    def test_cleanup_yes_skips_prompt_and_proceeds(self):
        runner = CliRunner()
        shard_module = _MockShardModule()

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "cleanup", "demo-shard", "--yes"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "Are you sure you want to cleanup this SHARD?" not in result.output
        assert "✓ Cleaned up SHARD: demo-shard" in result.output
        assert shard_module.cleanup_calls == [
            {
                "worktree_name": "demo-shard",
                "keep_branch": False,
                "caller_cwd": str(Path.cwd()),
            }
        ]

    def test_cleanup_without_yes_aborts_cleanly_when_stdin_is_closed(self):
        runner = CliRunner()
        shard_module = _MockShardModule()

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(cli, ["shard", "cleanup", "demo-shard"], input=None)

        assert result.exit_code == 1
        assert "Are you sure you want to cleanup this SHARD?" in result.output
        assert "Aborted!" in result.output
        assert shard_module.cleanup_calls == []


def _make_tender_shard_module(project_name="warp"):
    """Return a mock shard module wired up for tender tests."""
    m = MagicMock()
    m.get_shard_status.return_value = {
        "worktree_path": f"/home/patrick/projects/{project_name}/worktrees/demo-shard-001",
    }
    m.get_tender_metadata.return_value = {
        "last_commit_message": "Add feature X",
        "files_modified": ["foo.py"],
        "commits": 1,
        "branch_name": f"shard-demo-shard-001",
        "name": "demo-shard-001",
    }
    return m


def _make_request_dispatcher(available_site_ids, folio_result=None):
    """Return a make_request mock that serves /sites and /folios."""
    sites_payload = [{"site_id": sid} for sid in available_site_ids]
    folio_payload = folio_result or {"folio_id": "tender-20260101-test1"}

    def dispatch(method, endpoint, base_url, agent_id, **kwargs):
        if method == "GET" and endpoint == "/sites":
            return sites_payload
        if method == "POST" and endpoint == "/folios":
            return folio_payload
        return {}

    return dispatch


class TestShardTenderSiteValidation:
    def test_derived_site_missing_no_fallback_errors_with_available_list(self):
        """When warp-development and shard-review both absent, error with available sites."""
        runner = CliRunner()
        shard_module = _make_tender_shard_module("warp")
        dispatch = _make_request_dispatcher(["build", "portfolio-theses"])

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", side_effect=dispatch),
        ):
            result = runner.invoke(
                cli, ["shard", "tender", "demo-shard-001"], catch_exceptions=False
            )

        assert result.exit_code != 0
        assert "warp-development" in result.output
        assert "shard-review" in result.output
        assert "build" in result.output
        assert "portfolio-theses" in result.output
        assert "--site" in result.output

    def test_derived_site_missing_falls_through_to_shard_review(self):
        """When warp-development is absent but shard-review exists, use shard-review."""
        runner = CliRunner()
        shard_module = _make_tender_shard_module("warp")
        dispatch = _make_request_dispatcher(["build", "shard-review"])

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", side_effect=dispatch),
        ):
            result = runner.invoke(
                cli, ["shard", "tender", "demo-shard-001"], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        assert "shard-review" in result.output

    def test_derived_site_exists_is_used(self):
        """When skein-development exists, it is used without error."""
        runner = CliRunner()
        shard_module = _make_tender_shard_module("skein")
        dispatch = _make_request_dispatcher(["skein-development", "skein-dev"])

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", side_effect=dispatch),
        ):
            result = runner.invoke(
                cli, ["shard", "tender", "demo-shard-001"], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        assert "skein-development" in result.output

    def test_explicit_site_missing_errors_with_available_list(self):
        """When --site names a non-existent site, error with available sites."""
        runner = CliRunner()
        shard_module = _make_tender_shard_module("warp")
        dispatch = _make_request_dispatcher(["build", "portfolio-theses"])

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", side_effect=dispatch),
        ):
            result = runner.invoke(
                cli,
                ["shard", "tender", "demo-shard-001", "--site", "nonexistent-site"],
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        assert "nonexistent-site" in result.output
        assert "build" in result.output
        assert "portfolio-theses" in result.output

    def test_explicit_site_valid_succeeds(self):
        """When --site names an existing site, tender proceeds normally."""
        runner = CliRunner()
        shard_module = _make_tender_shard_module("warp")
        dispatch = _make_request_dispatcher(["build", "portfolio-theses"])

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", side_effect=dispatch),
        ):
            result = runner.invoke(
                cli,
                ["shard", "tender", "demo-shard-001", "--site", "build"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "build" in result.output
