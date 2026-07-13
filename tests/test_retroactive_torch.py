"""Retroactive torch lifecycle and completion-time folio attribution."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from client.cli import (
    MAX_CEREMONY_FOLIO_TITLES,
    MAX_RETROACTIVE_FOLIOS,
    _show_folio_inventory,
    cli,
)


def _invoke(args, request, **patches):
    env = {"SKEIN_AGENT_ID": "", "SKEIN_CHAIN_ID": "", "SKEIN_CHAIN_TASK": ""}
    with patch("client.cli.make_request", side_effect=request), patch(
        "client.cli.get_base_url", return_value="http://skein.test"
    ):
        if "generated_name" in patches:
            with patch(
                "client.cli._generate_suggested_name",
                return_value=patches["generated_name"],
            ):
                return CliRunner().invoke(cli, args, env=env)
        return CliRunner().invoke(cli, args, env=env)


def test_torch_without_agent_has_retroactive_breadcrumb():
    def no_request(*_args, **_kwargs):
        raise AssertionError("missing-agent torch must fail before an API request")

    result = _invoke(["torch"], no_request)

    assert result.exit_code != 0
    assert "Must set SKEIN_AGENT_ID or use --agent flag" in result.output
    assert "skein torch --retroactive" in result.output


def test_torch_preview_without_agent_keeps_recovery_breadcrumb():
    def no_request(*_args, **_kwargs):
        raise AssertionError("missing-agent preview must fail before an API request")

    result = _invoke(["torch", "--preview"], no_request)

    assert result.exit_code != 0
    assert "Must set SKEIN_AGENT_ID or use --agent flag" in result.output
    assert "skein torch --retroactive" in result.output


def test_unknown_roster_agent_has_retroactive_breadcrumb():
    def request(method, endpoint, *_args, **_kwargs):
        assert method == "GET" and endpoint == "/roster/unregistered-agent"
        raise RuntimeError("404")

    result = _invoke(["--agent", "unregistered-agent", "torch"], request)

    assert result.exit_code != 0
    assert "not found in roster" in result.output
    assert "skein torch --retroactive" in result.output


@pytest.mark.parametrize("flag", ["--preview", "--dry-run"])
def test_torch_preview_renders_real_ceremony_without_writes(flag):
    agent_id = "ember-badger-0712"
    calls = []

    def request(method, endpoint, _base_url, request_agent, **kwargs):
        assert request_agent == agent_id
        calls.append((method, endpoint, kwargs))
        assert method == "GET", "preview must not make any API write"
        if endpoint == f"/roster/{agent_id}":
            return {
                "name": agent_id,
                "status": "active",
                "metadata": {"ignited_from": "brief-20260712-start"},
            }
        if endpoint == "/folios/brief-20260712-start":
            return {
                "folio_id": "brief-20260712-start",
                "type": "brief",
                "title": "Improve the torch ceremony",
                "status": "open",
            }
        if endpoint == "/folios":
            return [
                {
                    "folio_id": "finding-20260712-work",
                    "type": "finding",
                    "title": "Preview must share the real renderer",
                    "created_by": agent_id,
                }
            ]
        if endpoint == "/threads":
            return []
        raise AssertionError((method, endpoint, kwargs))

    result = _invoke(["--agent", agent_id, "torch", flag], request)

    assert result.exit_code == 0, result.output
    assert "TORCH PREVIEW - Retirement Phase" in result.output
    assert "Preview only: roster state will not be changed." in result.output
    assert "Improve the torch ceremony [open] brief-20260712-start" in result.output
    assert "findings: 1" in result.output
    assert "Preview must share the real renderer finding-20260712-work" in result.output
    assert "consider what should survive this session" in result.output
    assert "Preview only: retirement has not been recorded." in result.output
    assert f"skein --agent {agent_id} torch" in result.output
    assert "skein complete" not in result.output
    assert calls and all(method == "GET" for method, *_ in calls)


@pytest.mark.parametrize("flag", ["--preview", "--dry-run"])
def test_torch_preview_rejects_retroactive_before_requests_or_name_generation(flag):
    def no_request(*_args, **_kwargs):
        raise AssertionError("incompatible flags must fail before an API request")

    with patch(
        "client.cli._generate_suggested_name",
        side_effect=AssertionError("must fail before generating a name"),
    ):
        result = _invoke(["torch", "--retroactive", flag], no_request)

    assert result.exit_code != 0
    assert "cannot be combined with --retroactive" in result.output
    assert "skein torch --retroactive" in result.output


def test_torch_help_lists_preview_and_dry_run_aliases():
    result = CliRunner().invoke(cli, ["torch", "--help"])

    assert result.exit_code == 0, result.output
    assert "--preview" in result.output
    assert "--dry-run" in result.output


def test_retroactive_torch_assigns_name_and_runs_normal_suggestions():
    calls = []

    def request(method, endpoint, _base_url, agent_id, **kwargs):
        calls.append((method, endpoint, agent_id, kwargs))
        if method == "POST" and endpoint == "/roster/register":
            return {"success": True}
        if method == "GET" and endpoint == "/folios":
            return []
        if method == "GET" and endpoint == "/threads":
            return []
        if method == "GET" and endpoint == "/roster/copper-owl-0712":
            return {"name": "copper-owl-0712", "metadata": {}}
        raise AssertionError((method, endpoint, kwargs))

    result = _invoke(["torch", "--retroactive"], request, generated_name="copper-owl-0712")

    assert result.exit_code == 0, result.output
    assert "Name: copper-owl-0712" in result.output
    assert "Session recovered: this work began without ignition." in result.output
    assert "no folios attributed yet" in result.output
    assert "consider what should survive this session" in result.output
    assert "skein post brief SITE" in result.output
    assert '--title "Continue the work"' in result.output
    assert '--title "Handoff: continue the work"' in result.output
    assert "brief-RELEVANT brief-HANDOFF reference" in result.output
    assert "skein --agent copper-owl-0712 complete FOLIO_ID..." in result.output
    assert f"up to {MAX_RETROACTIVE_FOLIOS} folios" in result.output

    registrations = [call for call in calls if call[:2] == ("POST", "/roster/register")]
    assert len(registrations) == 1
    assert registrations[0][2] == "copper-owl-0712"
    assert registrations[0][3]["json"]["status"] == "retiring"
    assert "retroactive_torch_at" in registrations[0][3]["json"]["metadata"]
    assert [call for call in calls if call[0] != "GET"] == registrations


def test_normal_torch_shows_counts_titles_and_closed_ignition_brief():
    agent_id = "ember-badger-0712"
    calls = []
    folios = [
        {
            "folio_id": "finding-20260712-abcd",
            "type": "finding",
            "title": "Retroactive torch belongs inside retirement",
            "created_by": agent_id,
        },
        {
            "folio_id": "brief-20260712-efgh",
            "type": "brief",
            "title": "Continue improving the ceremony",
            "created_by": agent_id,
        },
    ]

    def request(method, endpoint, _base_url, request_agent, **kwargs):
        assert request_agent == agent_id
        calls.append((method, endpoint, kwargs))
        if method == "GET" and endpoint == f"/roster/{agent_id}":
            return {
                "name": agent_id,
                "metadata": {"ignited_from": "brief-20260712-start"},
            }
        if method == "GET" and endpoint == "/folios/brief-20260712-start":
            return {
                "folio_id": "brief-20260712-start",
                "type": "brief",
                "title": "Implement the retirement ceremony",
                "status": "closed",
            }
        if method == "GET" and endpoint == "/folios":
            return folios
        if method == "GET" and endpoint == "/threads":
            return []
        if method == "PATCH" and endpoint == f"/roster/{agent_id}":
            return {"success": True}
        raise AssertionError((method, endpoint, kwargs))

    result = _invoke(["--agent", agent_id, "torch"], request)

    assert result.exit_code == 0, result.output
    assert "Implement the retirement ceremony [closed] brief-20260712-start" in result.output
    assert "findings: 1" in result.output
    assert "briefs: 1" in result.output
    assert "Work from this session:" in result.output
    assert "Retroactive torch belongs inside retirement finding-20260712-abcd" in result.output
    assert "Continue improving the ceremony brief-20260712-efgh" in result.output
    assert any(
        method == "PATCH"
        and endpoint == f"/roster/{agent_id}"
        and kwargs["json"] == {"status": "retiring"}
        for method, endpoint, kwargs in calls
    )
    assert not any(method == "POST" and endpoint == "/roster/register" for method, endpoint, _ in calls)
    assert [
        (method, endpoint) for method, endpoint, _ in calls if method != "GET"
    ] == [("PATCH", f"/roster/{agent_id}")]


def test_ignite_ready_torch_preserves_qualified_ignition_brief():
    agent_id = "ember-badger-0712"
    brief_ref = "speakbot:brief-20260712-start"
    brief = {
        "folio_id": "brief-20260712-start",
        "type": "brief",
        "title": "Carry the ceremony into the next session",
        "content": "Implement the retirement ceremony.",
        "status": "closed",
    }
    roster = {}

    def request(method, endpoint, _base_url, request_agent, **kwargs):
        if method == "GET" and endpoint == f"/folios/{brief_ref}":
            return brief
        if method == "POST" and endpoint == "/roster/register":
            payload = kwargs["json"]
            roster.clear()
            roster.update(payload)
            return {"success": True}
        if method == "GET" and endpoint == f"/roster/{agent_id}":
            return dict(roster)
        if method == "PATCH" and endpoint == f"/roster/{agent_id}":
            payload = kwargs["json"]
            roster.update({key: value for key, value in payload.items() if key != "metadata"})
            roster.setdefault("metadata", {}).update(payload.get("metadata", {}))
            return {"success": True}
        if method == "GET" and endpoint == "/folios":
            return []
        if method == "GET" and endpoint == "/threads":
            return []
        raise AssertionError((method, endpoint, request_agent, kwargs))

    ignited = _invoke(
        ["ignite", brief_ref], request, generated_name=agent_id
    )
    assert ignited.exit_code == 0, ignited.output
    assert roster["metadata"]["ignited_from"] == brief_ref

    readied = _invoke(["--agent", agent_id, "ready"], request)
    assert readied.exit_code == 0, readied.output
    assert roster["status"] == "active"
    assert roster["metadata"]["ignited_from"] == brief_ref
    assert "ready_at" in roster["metadata"]

    torched = _invoke(["--agent", agent_id, "torch"], request)
    assert torched.exit_code == 0, torched.output
    assert (
        "Carry the ceremony into the next session [closed] brief-20260712-start"
        in torched.output
    )
    assert roster["status"] == "retiring"
    assert roster["metadata"]["ignited_from"] == brief_ref


def test_ceremony_title_inventory_is_capped(capsys):
    folios = [
        {
            "folio_id": f"finding-20260712-{index:04d}",
            "type": "finding",
            "title": f"Finding number {index}",
        }
        for index in range(MAX_CEREMONY_FOLIO_TITLES + 1)
    ]

    _show_folio_inventory("Work from this session:", folios)
    output = capsys.readouterr().out

    assert output.count("Finding number ") == MAX_CEREMONY_FOLIO_TITLES
    assert "Finding number 20" not in output
    assert "...and 1 more" in output


def test_complete_attributes_any_folio_type_and_latest_record_is_effective():
    agent_id = "copper-owl-0712"
    folios = {
        "finding-20260712-one1": {
            "folio_id": "finding-20260712-one1",
            "type": "finding",
            "title": "Preserve the folio digest",
            "created_by": "unknown",
            "site_id": "skein-dev",
        },
        "mantle-20260712-two2": {
            "folio_id": "mantle-20260712-two2",
            "type": "mantle",
            "title": "Retirement guide",
            "created_by": "another-agent",
            "site_id": "skein-dev",
        },
        "brief-20260712-three3": {
            "folio_id": "brief-20260712-three3",
            "type": "brief",
            "title": "Continue the ceremony work",
            "created_by": agent_id,
            "site_id": "skein-dev",
        },
    }
    threads = [
        {
            "thread_id": "thread-old-mantle",
            "from_id": "mantle-20260712-two2",
            "to_id": "prior-agent",
            "type": "attribution",
            "created_at": "2026-07-12T10:00:00+00:00",
        },
        {
            "thread_id": "thread-old-brief",
            "from_id": "brief-20260712-three3",
            "to_id": "prior-agent",
            "type": "attribution",
            "created_at": "2026-07-12T10:01:00+00:00",
        },
    ]
    roster_patches = []

    def request(method, endpoint, _base_url, request_agent, **kwargs):
        assert request_agent == agent_id
        if method == "GET" and endpoint == f"/roster/{agent_id}":
            return {"name": agent_id, "metadata": {"retroactive_torch_at": "now"}}
        if method == "GET" and endpoint.startswith("/folios/"):
            return folios[endpoint.removeprefix("/folios/")]
        if method == "GET" and endpoint == "/folios":
            return list(folios.values())
        if method == "GET" and endpoint == "/threads":
            assert kwargs.get("params") == {"type": "attribution"}
            return list(threads)
        if method == "POST" and endpoint == "/threads":
            payload = kwargs["json"]
            threads.append(
                {
                    **payload,
                    "thread_id": f"thread-new-{len(threads)}",
                    "created_at": f"2026-07-12T11:0{len(threads)}:00+00:00",
                }
            )
            return {"success": True}
        if method == "PATCH" and endpoint == f"/roster/{agent_id}":
            roster_patches.append(kwargs["json"])
            return {"success": True}
        raise AssertionError((method, endpoint, kwargs))

    result = _invoke(
        [
            "--agent",
            agent_id,
            "complete",
            "finding-20260712-one1",
            "mantle-20260712-two2",
        ],
        request,
    )

    assert result.exit_code == 0, result.output
    assert f"Attributed 2 folio(s) to {agent_id}" in result.output
    assert "Preserve the folio digest finding-20260712-one1" in result.output
    assert "Retirement guide mantle-20260712-two2" in result.output
    assert "findings: 1" in result.output
    assert "mantles: 1" in result.output
    assert "briefs:" not in result.output
    assert [thread["from_id"] for thread in threads[-2:]] == [
        "finding-20260712-one1",
        "mantle-20260712-two2",
    ]
    assert all(thread["to_id"] == agent_id for thread in threads[-2:])
    assert all(thread["type"] == "attribution" for thread in threads[-2:])
    assert roster_patches[-1]["status"] == "retired"
    assert roster_patches[-1]["metadata"]["work_summary"]["mantles"] == 1


def test_complete_repeats_ignition_state_counts_and_titled_inventory():
    agent_id = "ember-badger-0712"
    session_folio = {
        "folio_id": "finding-20260712-work",
        "type": "finding",
        "title": "Ceremony titles restore the session story",
        "created_by": agent_id,
        "site_id": "skein-dev",
    }

    def request(method, endpoint, _base_url, request_agent, **kwargs):
        assert request_agent == agent_id
        if method == "GET" and endpoint == f"/roster/{agent_id}":
            return {
                "name": agent_id,
                "metadata": {"ignited_from": "brief-20260712-start"},
            }
        if method == "GET" and endpoint == "/folios/brief-20260712-start":
            return {
                "folio_id": "brief-20260712-start",
                "type": "brief",
                "title": "Improve the torch ceremony",
                "status": "open",
            }
        if method == "GET" and endpoint == "/folios":
            return [session_folio]
        if method == "GET" and endpoint == "/threads":
            return []
        if method == "PATCH" and endpoint == f"/roster/{agent_id}":
            return {"success": True}
        raise AssertionError((method, endpoint, kwargs))

    result = _invoke(["--agent", agent_id, "complete"], request)

    assert result.exit_code == 0, result.output
    assert "Improve the torch ceremony [open] brief-20260712-start" in result.output
    assert "Final Work Summary:" in result.output
    assert "findings: 1" in result.output
    assert "Left in SKEIN:" in result.output
    assert "Ceremony titles restore the session story finding-20260712-work" in result.output


def test_complete_rejects_more_than_25_folios_before_fetch_or_write():
    calls = []
    agent_id = "copper-owl-0712"

    def request(method, endpoint, _base_url, request_agent, **kwargs):
        calls.append((method, endpoint, request_agent, kwargs))
        if method == "GET" and endpoint == f"/roster/{agent_id}":
            return {"name": agent_id}
        raise AssertionError("folio requests must not start above the cap")

    ids = [f"finding-20260712-{index:04d}" for index in range(26)]
    result = _invoke(["--agent", agent_id, "complete", *ids], request)

    assert result.exit_code != 0
    assert "At most 25 folios" in result.output
    assert "received 26" in result.output
    assert [(method, endpoint) for method, endpoint, *_ in calls] == [
        ("GET", f"/roster/{agent_id}")
    ]


def test_complete_validates_entire_batch_before_attribution_writes():
    calls = []
    agent_id = "copper-owl-0712"

    def request(method, endpoint, _base_url, request_agent, **kwargs):
        calls.append((method, endpoint, request_agent, kwargs))
        if method == "GET" and endpoint == f"/roster/{agent_id}":
            return {"name": agent_id}
        if method == "GET" and endpoint == "/folios/finding-20260712-good":
            return {
                "folio_id": "finding-20260712-good",
                "type": "finding",
                "created_by": "unknown",
            }
        if method == "GET" and endpoint == "/folios/brief-20260712-missing":
            raise RuntimeError("404 folio not found")
        raise AssertionError((method, endpoint, kwargs))

    result = _invoke(
        [
            "--agent",
            agent_id,
            "complete",
            "finding-20260712-good",
            "brief-20260712-missing",
        ],
        request,
    )

    assert result.exit_code != 0
    assert "Could not attribute 1 folio(s)" in result.output
    assert "brief-20260712-missing" in result.output
    assert not any(method in ("POST", "PATCH") for method, *_ in calls)


def test_complete_stops_when_existing_attributions_cannot_be_loaded():
    calls = []
    agent_id = "copper-owl-0712"
    folio_id = "finding-20260712-good"

    def request(method, endpoint, _base_url, request_agent, **kwargs):
        calls.append((method, endpoint, request_agent, kwargs))
        if method == "GET" and endpoint == f"/roster/{agent_id}":
            return {"name": agent_id}
        if method == "GET" and endpoint == f"/folios/{folio_id}":
            return {"folio_id": folio_id, "type": "finding", "created_by": "unknown"}
        if method == "GET" and endpoint == "/threads":
            raise RuntimeError("attribution lookup unavailable")
        raise AssertionError((method, endpoint, kwargs))

    result = _invoke(["--agent", agent_id, "complete", folio_id], request)

    assert result.exit_code != 0
    assert "Could not load existing folio attributions" in result.output
    assert "attribution lookup unavailable" in result.output
    assert not any(method in ("POST", "PATCH") for method, *_ in calls)


def test_attribution_thread_round_trips_through_api(tmp_path):
    from fastapi.testclient import TestClient

    from skein.routes import get_project_store
    from skein.storage import JSONStore
    from skein_server import app

    store = JSONStore(tmp_path)
    app.dependency_overrides[get_project_store] = lambda: store
    client = TestClient(app)
    try:
        response = client.post(
            "/skein/threads",
            json={
                "from_id": "mantle-20260712-two2",
                "to_id": "copper-owl-0712",
                "type": "attribution",
                "content": "Authorship attributed during retroactive torch",
            },
            headers={"X-Agent-Id": "copper-owl-0712"},
        )
        assert response.status_code == 200, response.text

        response = client.get(
            "/skein/threads", params={"type": "attribution"}
        )
        assert response.status_code == 200, response.text
        assert response.json()[0]["from_id"] == "mantle-20260712-two2"
        assert response.json()[0]["to_id"] == "copper-owl-0712"
    finally:
        app.dependency_overrides.pop(get_project_store, None)
