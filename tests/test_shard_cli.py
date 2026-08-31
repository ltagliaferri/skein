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


class TestShardReviewCli:
    def test_worktree_name_dispatches_to_inspect(self):
        runner = CliRunner()
        shard_module = MagicMock()
        shard_module.ShardError = _MockShardModule.ShardError
        shard_module.get_shard_status.return_value = None

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["--agent", "reviewer", "shard", "review", "demo-shard"],
            )

        assert result.exit_code != 0
        assert "SHARD not found: demo-shard" in result.output
        shard_module.get_shard_status.assert_called_once_with("demo-shard")
        shard_module.get_review_queue.assert_not_called()

    def test_no_worktree_name_keeps_review_queue_behavior(self):
        runner = CliRunner()
        shard_module = MagicMock()
        shard_module.get_review_queue.return_value = {
            "ready": [],
            "needs_commit": [],
            "conflicts": [],
            "stale": [],
        }

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(cli, ["shard", "review"])

        assert result.exit_code == 0, result.output
        assert result.output == "No SHARDs found\n"
        shard_module.get_review_queue.assert_called_once_with(stale_days=7)
        shard_module.get_shard_status.assert_not_called()


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


def _make_where_shard_module(**overrides):
    """Return a mock shard module wired up for `shard where` tests."""
    m = MagicMock()

    class ShardError(Exception):
        pass

    m.ShardError = ShardError
    location = {
        "worktree_name": "demo-shard-20260101-001",
        "worktree_path": "/home/agent/.skein/worktrees/skein-ab12cd34/demo-shard-20260101-001",
        "worktrees_dir": "/home/agent/.skein/worktrees/skein-ab12cd34",
        "project_root": "/home/agent/projects/skein",
        "branch_name": "shard-demo-shard-20260101-001",
        "exists": True,
        "registered": True,
        "source": "git",
    }
    location.update(overrides)
    m.get_shard_location.return_value = location
    return m


class TestShardWhereCli:
    def test_where_prints_path_and_origin_repo(self):
        runner = CliRunner()
        shard_module = _make_where_shard_module()

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "where", "demo-shard-20260101-001"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "/home/agent/.skein/worktrees/skein-ab12cd34/demo-shard-20260101-001" in (
            result.output
        )
        assert "/home/agent/projects/skein" in result.output
        assert "shard-demo-shard-20260101-001" in result.output
        assert "⚠️" not in result.output

    def test_path_only_prints_a_bare_path(self):
        """WHY: The point of --path-only is `cd "$(skein shard where X --path-only)"`,
        which breaks if anything else is on stdout."""
        runner = CliRunner()
        shard_module = _make_where_shard_module()

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "where", "demo-shard-20260101-001", "--path-only"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert result.output.strip() == (
            "/home/agent/.skein/worktrees/skein-ab12cd34/demo-shard-20260101-001"
        )

    def test_json_output_is_the_full_location(self):
        import json

        runner = CliRunner()
        shard_module = _make_where_shard_module()

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "where", "demo-shard-20260101-001", "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["project_root"] == "/home/agent/projects/skein"
        assert payload["source"] == "git"

    def test_missing_worktree_is_flagged(self):
        runner = CliRunner()
        shard_module = _make_where_shard_module(
            exists=False, registered=False, source="expected"
        )

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "where", "demo-shard-20260101-001"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "does not exist" in result.output
        assert "expected" in result.output

    def test_unregistered_worktree_is_flagged(self):
        """WHY: A directory git no longer tracks is a different problem from a
        missing one, and needs a different fix."""
        runner = CliRunner()
        shard_module = _make_where_shard_module(
            exists=True, registered=False, source="database"
        )

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "where", "demo-shard-20260101-001"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "does not list it as a worktree" in result.output

    def test_shard_error_becomes_a_clean_cli_error(self):
        runner = CliRunner()
        shard_module = _make_where_shard_module()
        shard_module.get_shard_location.side_effect = shard_module.ShardError(
            "Worktree name is required"
        )

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(cli, ["shard", "where", "x"])

        assert result.exit_code != 0
        assert "Worktree name is required" in result.output


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
        "branch_name": "shard-demo-shard-001",
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
