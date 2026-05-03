"""CLI tests for SHARD commands."""

import sys
from pathlib import Path
from unittest.mock import patch

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
