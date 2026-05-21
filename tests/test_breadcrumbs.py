"""Tests for cross-project breadcrumb hints and --all flag."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent))

from client.cli import (
    cli,
    FIND_BREADCRUMB,
    FOLIO_NOT_FOUND_BREADCRUMB,
    ACTIVITY_BREADCRUMB,
)


# ---------------------------------------------------------------------------
# Footer presence / suppression
# ---------------------------------------------------------------------------


class TestFindFooter:
    def test_footer_shown_with_results(self):
        runner = CliRunner()
        fake_response = {
            "results": {
                "folios": {
                    "items": [
                        {
                            "folio_id": "brief-20260101-aaaa",
                            "type": "brief",
                            "site_id": "demo",
                            "title": "Test brief",
                            "status": "open",
                            "created_at": "2026-01-01T00:00:00",
                            "content": "hello world",
                        }
                    ],
                    "total": 1,
                }
            },
            "execution_time_ms": 5,
        }
        with patch("client.cli.make_request", return_value=fake_response):
            result = runner.invoke(cli, ["find", "hello"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert FIND_BREADCRUMB in result.output

    def test_footer_shown_with_zero_results(self):
        runner = CliRunner()
        fake_response = {"results": {"folios": {"items": [], "total": 0}}}
        with patch("client.cli.make_request", return_value=fake_response):
            result = runner.invoke(cli, ["find", "noresults"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "No folios found" in result.output
        assert FIND_BREADCRUMB in result.output

    def test_footer_not_shown_in_json(self):
        runner = CliRunner()
        fake_response = {"results": {"folios": {"items": [], "total": 0}}}
        with patch("client.cli.make_request", return_value=fake_response):
            result = runner.invoke(
                cli, ["find", "x", "--json"], catch_exceptions=False
            )
        assert result.exit_code == 0, result.output
        assert FIND_BREADCRUMB not in result.output
        # output should be valid JSON
        json.loads(result.output)

    def test_footer_not_shown_with_all(self):
        runner = CliRunner()
        with patch(
            "client.cli._load_projects_registry",
            return_value={"alpha": {"data_dir": "/tmp/a"}},
        ), patch(
            "client.cli._query_project",
            return_value={"results": {"folios": {"items": [], "total": 0}}},
        ):
            result = runner.invoke(cli, ["find", "x", "--all"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert FIND_BREADCRUMB not in result.output


class TestActivityFooter:
    def test_footer_shown(self):
        runner = CliRunner()
        with patch(
            "client.cli.make_request",
            return_value={"new_folios": [], "active_agents": []},
        ):
            result = runner.invoke(cli, ["activity"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert ACTIVITY_BREADCRUMB in result.output

    def test_footer_not_shown_in_json(self):
        runner = CliRunner()
        with patch(
            "client.cli.make_request",
            return_value={"new_folios": [], "active_agents": []},
        ):
            result = runner.invoke(
                cli, ["activity", "--json"], catch_exceptions=False
            )
        assert result.exit_code == 0, result.output
        assert ACTIVITY_BREADCRUMB not in result.output

    def test_footer_not_shown_with_all(self):
        runner = CliRunner()
        with patch(
            "client.cli._load_projects_registry",
            return_value={"alpha": {"data_dir": "/tmp/a"}},
        ), patch(
            "client.cli._query_project",
            return_value={"new_folios": [], "active_agents": []},
        ):
            result = runner.invoke(cli, ["activity", "--all"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert ACTIVITY_BREADCRUMB not in result.output


class TestSitesFooter:
    def test_footer_shown(self):
        runner = CliRunner()
        with patch(
            "client.cli._load_projects_registry",
            return_value={"alpha": {}, "beta": {}},
        ), patch("client.cli.make_request", return_value=[]), patch(
            "client.cli._current_project_id", return_value="alpha"
        ):
            result = runner.invoke(cli, ["sites"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        # Should mention the count of OTHER registered projects (1 here).
        assert "1 other project(s) registered" in result.output

    def test_footer_not_shown_in_json(self):
        runner = CliRunner()
        with patch(
            "client.cli._load_projects_registry",
            return_value={"alpha": {}, "beta": {}},
        ), patch("client.cli.make_request", return_value=[]), patch(
            "client.cli._current_project_id", return_value="alpha"
        ):
            result = runner.invoke(cli, ["sites", "--json"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "registered" not in result.output

    def test_footer_not_shown_with_all(self):
        runner = CliRunner()
        with patch(
            "client.cli._load_projects_registry",
            return_value={"alpha": {}, "beta": {}},
        ), patch("client.cli._query_project", return_value=[]):
            result = runner.invoke(cli, ["sites", "--all"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "registered —" not in result.output


class TestFolioNotFoundFooter:
    def test_breadcrumb_on_404(self):
        import click

        runner = CliRunner()
        with patch(
            "client.cli.make_request",
            side_effect=click.ClickException("API error: 404 Folio not found"),
        ):
            result = runner.invoke(
                cli, ["folio", "brief-20260101-zzzz"], catch_exceptions=False
            )
        assert result.exit_code != 0
        assert FOLIO_NOT_FOUND_BREADCRUMB in result.output

    def test_no_breadcrumb_in_json(self):
        import click

        runner = CliRunner()
        with patch(
            "client.cli.make_request",
            side_effect=click.ClickException("API error: 404 Folio not found"),
        ):
            result = runner.invoke(
                cli,
                ["folio", "brief-20260101-zzzz", "--json"],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert FOLIO_NOT_FOUND_BREADCRUMB not in result.output


# ---------------------------------------------------------------------------
# --all aggregation behaviour
# ---------------------------------------------------------------------------


class TestFindAll:
    def test_aggregates_across_projects(self):
        runner = CliRunner()
        registry = {
            "alpha": {"data_dir": "/tmp/a"},
            "beta": {"data_dir": "/tmp/b"},
        }

        responses = {
            "alpha": {
                "results": {
                    "folios": {
                        "items": [
                            {
                                "folio_id": "brief-20260101-aaaa",
                                "type": "brief",
                                "site_id": "core",
                                "title": "Alpha brief",
                                "status": "open",
                                "created_at": "2026-01-01T00:00:00",
                            }
                        ],
                        "total": 1,
                    }
                }
            },
            "beta": {
                "results": {
                    "folios": {
                        "items": [
                            {
                                "folio_id": "issue-20260102-bbbb",
                                "type": "issue",
                                "site_id": "ops",
                                "title": "Beta issue",
                                "status": "open",
                                "created_at": "2026-01-02T00:00:00",
                            }
                        ],
                        "total": 1,
                    }
                }
            },
        }

        def fake_query(project_id, *args, **kwargs):
            return responses[project_id]

        with patch(
            "client.cli._load_projects_registry", return_value=registry
        ), patch("client.cli._query_project", side_effect=fake_query):
            result = runner.invoke(cli, ["find", "--all"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "brief-20260101-aaaa" in result.output
        assert "issue-20260102-bbbb" in result.output
        assert "Found 2 folio(s) across 2 project(s)" in result.output

    def test_skips_unreachable_project(self):
        runner = CliRunner()
        registry = {
            "alpha": {"data_dir": "/tmp/a"},
            "broken": {"data_dir": "/nonexistent"},
        }

        def fake_query(project_id, *args, **kwargs):
            if project_id == "broken":
                return None
            return {
                "results": {
                    "folios": {
                        "items": [
                            {
                                "folio_id": "brief-20260101-aaaa",
                                "type": "brief",
                                "site_id": "core",
                                "title": "Alpha brief",
                                "status": "open",
                                "created_at": "2026-01-01T00:00:00",
                            }
                        ],
                        "total": 1,
                    }
                }
            }

        with patch(
            "client.cli._load_projects_registry", return_value=registry
        ), patch("client.cli._query_project", side_effect=fake_query):
            result = runner.invoke(cli, ["find", "--all"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        # broken project skipped entirely
        assert "broken" not in result.output


# Failure mode: a registered project endpoint returns 2xx with non-JSON body
# (e.g. a reverse-proxy health page in front of a missing route). resp.json()
# raises json.JSONDecodeError, which is NOT a RequestException — without an
# explicit catch the whole --all invocation crashes instead of skipping the
# project.
class TestQueryProjectFailSoft:
    def test_skips_project_with_non_json_2xx_response(self):
        from unittest.mock import MagicMock

        from client.cli import _query_project

        fake_response = MagicMock()
        fake_response.text = "not json"
        fake_response.raise_for_status.return_value = None
        fake_response.json.side_effect = json.JSONDecodeError(
            "Expecting value", "not json", 0
        )

        with patch("client.cli.requests.request", return_value=fake_response):
            result = _query_project(
                project_id="malformed",
                method="GET",
                endpoint="/find",
                base_url="http://localhost:8000",
                agent_id=None,
            )

        assert result is None


class TestActivityAll:
    def test_aggregates_with_project_tags(self):
        runner = CliRunner()
        registry = {"alpha": {}, "beta": {}}

        responses = {
            "alpha": {
                "new_folios": [
                    {
                        "folio_id": "brief-20260101-aaaa",
                        "type": "brief",
                        "title": "Alpha thing",
                        "created_at": "2026-01-01T00:00:00",
                    }
                ],
                "active_agents": ["agent-a"],
            },
            "beta": {
                "new_folios": [
                    {
                        "folio_id": "issue-20260102-bbbb",
                        "type": "issue",
                        "title": "Beta thing",
                        "created_at": "2026-01-02T00:00:00",
                    }
                ],
                "active_agents": ["agent-b"],
            },
        }

        def fake_query(project_id, *args, **kwargs):
            return responses[project_id]

        with patch(
            "client.cli._load_projects_registry", return_value=registry
        ), patch("client.cli._query_project", side_effect=fake_query):
            result = runner.invoke(cli, ["activity", "--all"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "[alpha]" in result.output
        assert "[beta]" in result.output
        assert "Recent activity across 2 project(s)" in result.output


class TestSitesAll:
    def test_lists_grouped_by_project(self):
        runner = CliRunner()
        registry = {"alpha": {}, "beta": {}}
        responses = {
            "alpha": [
                {"site_id": "core", "purpose": "Core work", "status": "active"}
            ],
            "beta": [
                {"site_id": "ops", "purpose": "Operations", "status": "active"}
            ],
        }

        def fake_query(project_id, *args, **kwargs):
            return responses[project_id]

        with patch(
            "client.cli._load_projects_registry", return_value=registry
        ), patch("client.cli._query_project", side_effect=fake_query):
            result = runner.invoke(cli, ["sites", "--all"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "core" in result.output
        assert "ops" in result.output


class TestFolioAll:
    def test_returns_match_from_other_project(self):
        runner = CliRunner()
        registry = {"alpha": {}, "beta": {}}

        responses = {
            "alpha": None,  # not found in alpha
            "beta": {
                "folio_id": "brief-20260101-aaaa",
                "type": "brief",
                "site": "core",
                "title": "Found",
                "content": "the body",
                "created_at": "2026-01-01T00:00:00",
                "created_by": "tester",
                "status": "open",
            },
        }

        def fake_query(project_id, *args, **kwargs):
            return responses[project_id]

        with patch(
            "client.cli._load_projects_registry", return_value=registry
        ), patch("client.cli._query_project", side_effect=fake_query), patch(
            "client.cli.make_request", return_value=[]
        ):
            result = runner.invoke(
                cli,
                ["folio", "brief-20260101-aaaa", "--all", "--no-pager"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "brief-20260101-aaaa" in result.output
        # should be tagged with the source project
        assert "[beta]" in result.output

    def test_errors_when_not_in_any_project(self):
        runner = CliRunner()
        registry = {"alpha": {}, "beta": {}}

        with patch(
            "client.cli._load_projects_registry", return_value=registry
        ), patch("client.cli._query_project", return_value=None):
            result = runner.invoke(
                cli,
                ["folio", "brief-20260101-zzzz", "--all"],
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        assert "not found in any registered project" in result.output


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
