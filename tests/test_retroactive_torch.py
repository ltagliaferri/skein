"""Retroactive torch lifecycle and completion-time folio attribution."""

from unittest.mock import patch

from click.testing import CliRunner

from client.cli import MAX_RETROACTIVE_FOLIOS, cli


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


def test_unknown_roster_agent_has_retroactive_breadcrumb():
    def request(method, endpoint, *_args, **_kwargs):
        assert method == "GET" and endpoint == "/roster/unregistered-agent"
        raise RuntimeError("404")

    result = _invoke(["--agent", "unregistered-agent", "torch"], request)

    assert result.exit_code != 0
    assert "not found in roster" in result.output
    assert "skein torch --retroactive" in result.output


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
    assert "Before completing retirement, consider:" in result.output
    assert "skein --agent copper-owl-0712 complete FOLIO_ID..." in result.output
    assert f"up to {MAX_RETROACTIVE_FOLIOS} folios" in result.output

    registrations = [call for call in calls if call[:2] == ("POST", "/roster/register")]
    assert len(registrations) == 1
    assert registrations[0][2] == "copper-owl-0712"
    assert registrations[0][3]["json"]["status"] == "retiring"
    assert "retroactive_torch_at" in registrations[0][3]["json"]["metadata"]


def test_complete_attributes_any_folio_type_and_latest_record_is_effective():
    agent_id = "copper-owl-0712"
    folios = {
        "finding-20260712-one1": {
            "folio_id": "finding-20260712-one1",
            "type": "finding",
            "created_by": "unknown",
            "site_id": "skein-dev",
        },
        "mantle-20260712-two2": {
            "folio_id": "mantle-20260712-two2",
            "type": "mantle",
            "created_by": "another-agent",
            "site_id": "skein-dev",
        },
        "brief-20260712-three3": {
            "folio_id": "brief-20260712-three3",
            "type": "brief",
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
