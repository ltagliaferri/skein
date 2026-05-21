"""Tests for cross-project CLI features.

Covers:
- Top-level --project flag (P3)
- SKEIN_PROJECT env var (P3 / pre-existing)
- 'project:site' colon syntax on `skein post` commands (P2)
- Precedence: colon-syntax > --project flag > SKEIN_PROJECT > cwd .skein/
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent))

from client.cli import cli, parse_post_site_id


MOCK_RESULT = {"folio_id": "finding-20260101-test1"}


@pytest.fixture
def isolated_env():
    """Run each test with SKEIN_PROJECT cleared and restored."""
    saved = os.environ.pop("SKEIN_PROJECT", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["SKEIN_PROJECT"] = saved
        else:
            os.environ.pop("SKEIN_PROJECT", None)


def _last_call_headers_and_body(mock_request):
    """Pull headers and json body from the most recent make_request call.

    Headers in the CLI come from kwargs.pop('headers', {}) inside make_request,
    so we have to inspect through a small wrapper.
    """
    _, kwargs = mock_request.call_args
    return kwargs


class TestParsePostSiteId:
    """Unit-level test for the address parser."""

    def test_bare_site_id(self):
        site_id, project = parse_post_site_id("skein-dev")
        assert site_id == "skein-dev"
        assert project is None

    def test_project_qualified(self):
        site_id, project = parse_post_site_id("speakbot:skein-dev")
        assert site_id == "skein-dev"
        assert project == "speakbot"

    def test_no_colon(self):
        site_id, project = parse_post_site_id("simple-site")
        assert site_id == "simple-site"
        assert project is None


class TestColonSyntaxOnPost:
    """P2: post commands accept project:site colon syntax."""

    def _run_post(self, runner, mock_request, args):
        with patch("client.cli.make_request", mock_request):
            return runner.invoke(cli, args, catch_exceptions=False)

    def test_post_finding_bare_site(self, isolated_env):
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        result = self._run_post(
            runner,
            mock_request,
            ["post", "finding", "skein-dev", "Bare site finding"],
        )
        assert result.exit_code == 0, result.output
        kwargs = _last_call_headers_and_body(mock_request)
        # Bare site_id => no project_id override
        assert kwargs.get("project_id") is None
        assert kwargs["json"]["site_id"] == "skein-dev"

    def test_post_finding_qualified_site(self, isolated_env):
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        result = self._run_post(
            runner,
            mock_request,
            ["post", "finding", "speakbot:skein-dev", "Cross-project finding"],
        )
        assert result.exit_code == 0, result.output
        kwargs = _last_call_headers_and_body(mock_request)
        assert kwargs.get("project_id") == "speakbot"
        # The colon prefix is stripped from the body's site_id
        assert kwargs["json"]["site_id"] == "skein-dev"

    def test_post_friction_qualified_site(self, isolated_env):
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        result = self._run_post(
            runner,
            mock_request,
            ["post", "friction", "speakbot:skein-dev", "Cross-project friction"],
        )
        assert result.exit_code == 0, result.output
        kwargs = _last_call_headers_and_body(mock_request)
        assert kwargs.get("project_id") == "speakbot"
        assert kwargs["json"]["site_id"] == "skein-dev"

    def test_post_brief_qualified_site(self, isolated_env):
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        result = self._run_post(
            runner,
            mock_request,
            [
                "post",
                "brief",
                "speakbot:skein-dev",
                "Cross-project brief content",
                "--title",
                "T",
            ],
        )
        assert result.exit_code == 0, result.output
        kwargs = _last_call_headers_and_body(mock_request)
        assert kwargs.get("project_id") == "speakbot"
        assert kwargs["json"]["site_id"] == "skein-dev"

    def test_post_issue_qualified_site(self, isolated_env):
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        result = self._run_post(
            runner,
            mock_request,
            ["post", "issue", "speakbot:skein-dev", "Cross-project issue"],
        )
        assert result.exit_code == 0, result.output
        kwargs = _last_call_headers_and_body(mock_request)
        assert kwargs.get("project_id") == "speakbot"
        assert kwargs["json"]["site_id"] == "skein-dev"

    def test_post_notion_qualified_site(self, isolated_env):
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        result = self._run_post(
            runner,
            mock_request,
            ["post", "notion", "speakbot:skein-dev", "Cross-project notion"],
        )
        assert result.exit_code == 0, result.output
        kwargs = _last_call_headers_and_body(mock_request)
        assert kwargs.get("project_id") == "speakbot"

    def test_post_summary_qualified_site(self, isolated_env):
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        result = self._run_post(
            runner,
            mock_request,
            ["post", "summary", "speakbot:skein-dev", "Cross-project summary"],
        )
        assert result.exit_code == 0, result.output
        kwargs = _last_call_headers_and_body(mock_request)
        assert kwargs.get("project_id") == "speakbot"

    def test_playbook_create_qualified_site(self, isolated_env):
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        result = self._run_post(
            runner,
            mock_request,
            [
                "playbook",
                "create",
                "speakbot:skein-dev",
                "Some playbook content here",
                "--title",
                "T",
            ],
        )
        assert result.exit_code == 0, result.output
        kwargs = _last_call_headers_and_body(mock_request)
        assert kwargs.get("project_id") == "speakbot"
        assert kwargs["json"]["site_id"] == "skein-dev"


class TestTopLevelProjectFlag:
    """P3: --project on the top-level group works on every command."""

    def test_project_flag_on_post_command(self, isolated_env):
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        with patch("client.cli.make_request", mock_request):
            result = runner.invoke(
                cli,
                [
                    "--project",
                    "speakbot",
                    "post",
                    "finding",
                    "skein-dev",
                    "Finding via flag",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        # --project is pushed into SKEIN_PROJECT env var, so make_request picks
        # it up via the env path even when no explicit project_id kwarg is set.
        assert os.environ.get("SKEIN_PROJECT") == "speakbot"

    def test_project_flag_on_read_command(self, isolated_env):
        runner = CliRunner()
        mock_request = MagicMock(
            return_value=[{"site_id": "x", "purpose": "y", "status": "active"}]
        )
        with patch("client.cli.make_request", mock_request):
            result = runner.invoke(
                cli,
                ["--project", "speakbot", "sites"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert os.environ.get("SKEIN_PROJECT") == "speakbot"

    def test_skein_project_env_var_via_flag(self, isolated_env):
        """The --project click option also reads SKEIN_PROJECT envvar."""
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        with patch.dict(os.environ, {"SKEIN_PROJECT": "speakbot"}, clear=False):
            with patch("client.cli.make_request", mock_request):
                result = runner.invoke(
                    cli,
                    ["post", "finding", "skein-dev", "Finding via env"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, result.output

    def test_colon_syntax_overrides_project_flag(self, isolated_env):
        """Per-call colon syntax wins over --project flag."""
        runner = CliRunner()
        mock_request = MagicMock(return_value=MOCK_RESULT)
        with patch("client.cli.make_request", mock_request):
            result = runner.invoke(
                cli,
                [
                    "--project",
                    "speakbot",
                    "post",
                    "finding",
                    "skein:other-site",
                    "Per-call override",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        kwargs = _last_call_headers_and_body(mock_request)
        # Explicit project_id from colon syntax beats --project flag (which
        # only goes into env var, used as fallback inside make_request).
        assert kwargs.get("project_id") == "skein"
        assert kwargs["json"]["site_id"] == "other-site"


class TestMakeRequestProjectIdResolution:
    """The make_request precedence: explicit kwarg > env > cwd config."""

    def test_explicit_project_id_kwarg_wins(self, isolated_env):
        from client.cli import make_request

        with patch.dict(os.environ, {"SKEIN_PROJECT": "envproj"}, clear=False):
            captured = {}

            def fake_request(method, url, headers=None, **kwargs):
                captured["headers"] = headers or {}
                resp = MagicMock()
                resp.text = "{}"
                resp.json.return_value = {}
                resp.ok = True
                resp.raise_for_status = lambda: None
                return resp

            with patch("client.cli.requests.request", side_effect=fake_request):
                make_request(
                    "GET",
                    "/sites",
                    "http://localhost:8001",
                    "agent-x",
                    project_id="explicitproj",
                )

        assert captured["headers"].get("X-Project-Id") == "explicitproj"

    def test_env_var_used_when_no_explicit_kwarg(self, isolated_env):
        from client.cli import make_request

        with patch.dict(os.environ, {"SKEIN_PROJECT": "envproj"}, clear=False):
            captured = {}

            def fake_request(method, url, headers=None, **kwargs):
                captured["headers"] = headers or {}
                resp = MagicMock()
                resp.text = "{}"
                resp.json.return_value = {}
                resp.ok = True
                resp.raise_for_status = lambda: None
                return resp

            with patch("client.cli.requests.request", side_effect=fake_request):
                make_request("GET", "/sites", "http://localhost:8001", "agent-x")

        assert captured["headers"].get("X-Project-Id") == "envproj"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
