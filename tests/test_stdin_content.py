"""Tests for stdin content reading in SKEIN CLI commands."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent))

from client.cli import cli


MOCK_BRIEF_RESULT = {"folio_id": "brief-20260101-test1"}


class TestPostBriefStdin:
    """Tests for stdin reading in `skein post brief` and `skein brief create`."""

    def _make_mock_request(self):
        """Return a mock make_request that returns a valid brief result."""
        mock = MagicMock(return_value=MOCK_BRIEF_RESULT)
        return mock

    def test_post_brief_reads_stdin_when_content_is_dash(self):
        """When content arg is '-', post brief reads body from stdin."""
        runner = CliRunner()
        mock_request = self._make_mock_request()

        with patch("client.cli.make_request", mock_request):
            result = runner.invoke(
                cli,
                ["post", "brief", "test-site", "-", "--title", "Test Title"],
                input="This is the brief content from stdin.\nLine two.",
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "Created brief:" in result.output

        _, kwargs = mock_request.call_args
        posted_data = kwargs["json"]
        assert (
            posted_data["content"] == "This is the brief content from stdin.\nLine two."
        )
        assert posted_data["title"] == "Test Title"
        assert posted_data["site_id"] == "test-site"

    def test_post_brief_uses_inline_content_when_not_dash(self):
        """When content arg is a normal string, it is used as-is."""
        runner = CliRunner()
        mock_request = self._make_mock_request()

        with patch("client.cli.make_request", mock_request):
            result = runner.invoke(
                cli,
                [
                    "post",
                    "brief",
                    "test-site",
                    "Inline content",
                    "--title",
                    "Test Title",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output

        _, kwargs = mock_request.call_args
        posted_data = kwargs["json"]
        assert posted_data["content"] == "Inline content"

    def test_brief_create_reads_stdin_when_content_is_dash(self):
        """Deprecated `brief create` also reads stdin when content is '-'."""
        runner = CliRunner()
        mock_request = self._make_mock_request()

        with patch("client.cli.make_request", mock_request):
            result = runner.invoke(
                cli,
                ["brief", "create", "test-site", "-", "--title", "Handoff Title"],
                input="Heredoc content here.\nMultiple lines.",
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output

        _, kwargs = mock_request.call_args
        posted_data = kwargs["json"]
        assert posted_data["content"] == "Heredoc content here.\nMultiple lines."
        assert posted_data["title"] == "Handoff Title"

    def test_post_brief_stdin_content_multiline(self):
        """Multiline heredoc content is preserved in full."""
        runner = CliRunner()
        mock_request = self._make_mock_request()
        multiline = "Line 1\nLine 2\nLine 3\n\nSection 2\nMore content here."

        with patch("client.cli.make_request", mock_request):
            result = runner.invoke(
                cli,
                ["post", "brief", "test-site", "-", "--title", "Multi"],
                input=multiline,
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output

        _, kwargs = mock_request.call_args
        assert kwargs["json"]["content"] == multiline

    def test_post_brief_stdin_empty_input(self):
        """Empty stdin results in empty string content (not '-')."""
        runner = CliRunner()
        mock_request = self._make_mock_request()

        with patch("client.cli.make_request", mock_request):
            result = runner.invoke(
                cli,
                ["post", "brief", "test-site", "-", "--title", "Empty"],
                input="",
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output

        _, kwargs = mock_request.call_args
        assert kwargs["json"]["content"] == ""

    def test_post_brief_stdin_with_equals_sign_not_rejected(self):
        """Stdin content like 'title=My Brief' should NOT trigger validation error."""
        runner = CliRunner()
        mock_request = self._make_mock_request()

        with patch("client.cli.make_request", mock_request):
            result = runner.invoke(
                cli,
                ["post", "brief", "test-site", "-", "--title", "Test"],
                input="title=My Brief\nfoo=bar",
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output

        _, kwargs = mock_request.call_args
        assert kwargs["json"]["content"] == "title=My Brief\nfoo=bar"
