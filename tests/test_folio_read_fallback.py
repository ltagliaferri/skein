"""Regression tests for the folio-read truncation fallback.

The harness layer (Claude Code, codex, kimi, etc.) caps bash tool output at
varying sizes. Skein cannot remove that cap, but `skein folio` provides two
escape hatches that this suite locks in:

1. A header marker at the top of long-folio output naming total content
   length and a fallback command. Top placement so it survives harness
   truncation, which usually cuts from the end.
2. A `--raw` flag that emits only the content, suitable for piping to a file.

If a future change re-orders output so the marker no longer leads, or breaks
`--raw` as a clean content stream, these tests fail.
"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from client.cli import FOLIO_FALLBACK_HINT_THRESHOLD, cli


def _folio_payload(content: str, folio_id: str = "brief-20260501-test") -> dict:
    return {
        "folio_id": folio_id,
        "type": "brief",
        "site_id": "test-site",
        "created_at": "2026-05-01T00:00:00",
        "created_by": "test-agent",
        "title": "Test Brief",
        "content": content,
        "status": "open",
        "metadata": {},
    }


@pytest.fixture
def fake_request():
    """Stub make_request so CLI tests don't need a live server."""

    def factory(content: str, folio_id: str = "brief-20260501-test"):
        payload = _folio_payload(content, folio_id)

        def _impl(method, endpoint, *_args, **_kwargs):
            if endpoint.startswith("/folios/"):
                return payload
            if endpoint == "/threads":
                return []
            return {}

        return _impl

    return factory


def _invoke(cmd_args, fake_impl):
    """Invoke the cli group with a stubbed make_request."""
    with patch("client.cli.make_request", side_effect=fake_impl):
        return CliRunner().invoke(cli, cmd_args)


class TestFolioFallbackHeader:
    def test_long_folio_gets_fallback_header_at_top(self, fake_request):
        content = "x" * (FOLIO_FALLBACK_HINT_THRESHOLD + 100)
        result = _invoke(
            ["folio", "brief-20260501-test", "--no-pager"], fake_request(content)
        )

        assert result.exit_code == 0, result.output
        first_line = result.output.splitlines()[0]
        # Must lead with the fallback marker so the agent sees it before any
        # harness-side truncation kicks in.
        assert first_line.startswith("[folio brief-20260501-test:")
        assert str(len(content)) in first_line
        assert "--raw" in first_line

    def test_short_folio_skips_header(self, fake_request):
        content = "short content"
        result = _invoke(
            ["folio", "brief-20260501-test", "--no-pager"], fake_request(content)
        )

        assert result.exit_code == 0, result.output
        # No marker noise on small folios.
        assert not result.output.startswith("[folio")

    def test_header_threshold_boundary(self, fake_request):
        """Just-at-threshold gets the header; just-below does not."""
        at = "y" * FOLIO_FALLBACK_HINT_THRESHOLD
        below = "y" * (FOLIO_FALLBACK_HINT_THRESHOLD - 1)

        r_at = _invoke(["folio", "brief-x", "--no-pager"], fake_request(at))
        r_below = _invoke(["folio", "brief-x", "--no-pager"], fake_request(below))

        assert r_at.output.startswith("[folio")
        assert not r_below.output.startswith("[folio")


class TestFolioRaw:
    def test_raw_emits_only_content(self, fake_request):
        content = "# Heading\n\nbody body body\n"
        result = _invoke(
            ["folio", "brief-20260501-test", "--raw"], fake_request(content)
        )

        assert result.exit_code == 0, result.output
        # Exact equality: no header, no metadata, no thread block, no extra newline.
        assert result.output == content

    def test_raw_preserves_full_content_for_huge_folio(self, fake_request):
        # 200K — far above any plausible harness cap. --raw must hand back
        # every byte; that's its whole job as a fallback.
        content = "lorem ipsum dolor sit amet " * 8000
        result = _invoke(
            ["folio", "brief-20260501-test", "--raw"], fake_request(content)
        )

        assert result.exit_code == 0, result.output
        assert result.output == content

    def test_raw_skips_fallback_header(self, fake_request):
        # --raw must be clean for piping; the header would corrupt the file.
        content = "x" * (FOLIO_FALLBACK_HINT_THRESHOLD + 500)
        result = _invoke(
            ["folio", "brief-20260501-test", "--raw"], fake_request(content)
        )

        assert "[folio" not in result.output
        assert "--raw" not in result.output


class TestShowAliasInheritsFallback:
    def test_show_alias_propagates_raw(self, fake_request):
        content = "raw via show alias"
        result = _invoke(
            ["show", "brief-20260501-test", "--raw"], fake_request(content)
        )

        assert result.exit_code == 0, result.output
        assert result.output == content

    def test_show_alias_propagates_header(self, fake_request):
        content = "z" * (FOLIO_FALLBACK_HINT_THRESHOLD + 100)
        result = _invoke(
            ["show", "brief-20260501-test", "--no-pager"], fake_request(content)
        )

        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[0].startswith("[folio")
