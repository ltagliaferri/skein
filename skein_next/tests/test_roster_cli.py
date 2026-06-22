"""Stage 2 CLI — ignite/ready/torch/complete, the name guard, and chain yields.

Drives the ported lifecycle verbs through the click CLI against a local station,
no server — the surface mill and agents actually call.
"""

import pytest
from click.testing import CliRunner

from skein_next.cli import cli


def _run(data_dir, *args, env=None):
    return CliRunner().invoke(cli, ["--data-dir", str(data_dir), *args], env=env)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / ".skein-next"


def test_ignite_ready_torch_complete_round_trip(data_dir):
    r = _run(data_dir, "ignite", "--name", "nub-0622", "--message", "stage 2")
    assert r.exit_code == 0, r.output
    assert "You are: nub-0622" in r.output

    r = _run(data_dir, "ready", "--agent", "nub-0622")
    assert r.exit_code == 0, r.output
    assert "nub-0622: active" in r.output

    r = _run(data_dir, "torch", "--agent", "nub-0622")
    assert r.exit_code == 0, r.output
    assert "nub-0622: retiring" in r.output

    r = _run(data_dir, "complete", "--agent", "nub-0622")
    assert r.exit_code == 0, r.output
    assert "nub-0622: retired" in r.output


def test_agent_identity_from_env(data_dir):
    env = {"SKEIN_NEXT_AGENT": "morse-0621"}
    assert _run(data_dir, "ignite", "--name", "morse-0621", env=env).exit_code == 0
    # ready with no --agent falls back to $SKEIN_NEXT_AGENT.
    r = _run(data_dir, "ready", env=env)
    assert r.exit_code == 0, r.output
    assert "morse-0621: active" in r.output


def test_name_collision_rejected_via_cli(data_dir):
    assert _run(data_dir, "ignite", "--name", "nub-0622", "--agent", "alice").exit_code == 0
    r = _run(data_dir, "ignite", "--name", "nub-0622", "--agent", "bob")
    assert r.exit_code != 0
    assert "already taken" in r.output


def test_ready_twice_rejected_via_cli(data_dir):
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    r = _run(data_dir, "ready", "--agent", "nub-0622")
    assert r.exit_code != 0
    assert "illegal lifecycle transition" in r.output


def test_no_agent_identity_is_clear_error(data_dir):
    r = _run(data_dir, "ready")  # no --agent, no env
    assert r.exit_code != 0
    assert "no agent identity" in r.output


def test_complete_stores_and_reads_back_a_chain_yield(data_dir):
    env = {"SKEIN_CHAIN_ID": "chain-7", "SKEIN_CHAIN_TASK": "task-a"}
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    _run(data_dir, "torch", "--agent", "nub-0622")
    r = _run(
        data_dir, "complete", "--agent", "nub-0622",
        "--yield-status", "complete", "--yield-outcome", "did the work",
        env=env,
    )
    assert r.exit_code == 0, r.output
    assert "Yield stored: sack-" in r.output

    # The next task reads the chain's sack back.
    r = _run(data_dir, "chain", "yields", "chain-7")
    assert r.exit_code == 0, r.output
    assert "complete: did the work" in r.output
    assert "task task-a" in r.output


def test_complete_without_chain_just_retires(data_dir):
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    _run(data_dir, "torch", "--agent", "nub-0622")
    r = _run(data_dir, "complete", "--agent", "nub-0622")
    assert r.exit_code == 0, r.output
    assert "Yield stored" not in r.output
    assert "nub-0622: retired" in r.output


def test_complete_with_summary_posts_to_working_site(data_dir):
    _run(data_dir, "site", "create", "proj", "--purpose", "the work")
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    _run(data_dir, "post", "finding", "proj", "did a thing", "--by", "nub-0622")
    _run(data_dir, "torch", "--agent", "nub-0622")
    r = _run(data_dir, "complete", "--agent", "nub-0622", "--summary", "wrapped up the audit")
    assert r.exit_code == 0, r.output
    # The summary landed in the agent's working site (proj), not the roster.
    r = _run(data_dir, "folios", "proj", "--type", "summary")
    assert "wrapped up the audit" in r.output


def test_roster_and_activity_listing(data_dir):
    _run(data_dir, "ignite", "--name", "nub-0622", "--type", "claude-code")
    _run(data_dir, "ready", "--agent", "nub-0622")

    r = _run(data_dir, "roster")
    assert r.exit_code == 0, r.output
    assert "nub-0622 — active" in r.output
    assert "[claude-code]" in r.output

    r = _run(data_dir, "activity", "--json")
    assert r.exit_code == 0, r.output
    assert '"name": "nub-0622"' in r.output


def test_register_and_identify(data_dir):
    r = _run(data_dir, "register", "agent-007", "--type", "claude-code")
    assert r.exit_code == 0, r.output
    assert "Registered: agent-007" in r.output

    r = _run(data_dir, "identify", "agent-007", "--eval")
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "export SKEIN_NEXT_AGENT=agent-007"


def test_chain_yield_show(data_dir):
    env = {"SKEIN_CHAIN_ID": "chain-9", "SKEIN_CHAIN_TASK": "t1"}
    _run(data_dir, "ignite", "--name", "w")
    _run(data_dir, "ready", "--agent", "w")
    _run(data_dir, "torch", "--agent", "w")
    r = _run(data_dir, "complete", "--agent", "w", "--yield-outcome", "ok", env=env)
    sack_id = [w for w in r.output.split() if w.startswith("sack-")][0]
    r = _run(data_dir, "chain", "yield", sack_id)
    assert r.exit_code == 0, r.output
    assert sack_id in r.output
    assert "outcome: ok" in r.output
