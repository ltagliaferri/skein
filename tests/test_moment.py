"""First-class ``moment`` folio behavior across the API and CLI."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import ValidationError

from client.cli import FOLIO_SUMMARY_LABELS, cli
from skein.models import FolioCreate
from skein.routes import get_project_store, validate_folio_title
from skein.storage import JSONStore
from skein_server import app


@pytest.fixture
def client(tmp_path):
    store = JSONStore(tmp_path)
    app.dependency_overrides[get_project_store] = lambda: store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_project_store, None)


def _post_folio(client, folio_type, title, content):
    response = client.post(
        "/skein/folios",
        json={
            "type": folio_type,
            "site_id": "public-notes",
            "title": title,
            "content": content,
        },
        headers={"X-Agent-Id": "moment-tester"},
    )
    assert response.status_code == 200, response.text
    return response.json()["folio_id"]


def test_moment_model_and_title_validation():
    created = FolioCreate(
        type="moment",
        site_id="public-notes",
        title="Released the first public build",
        content="A public marker for the release.",
    )
    assert created.type == "moment"
    assert validate_folio_title("Moment: Released the first public build", "moment") == (
        "Released the first public build"
    )

    with pytest.raises(ValidationError):
        FolioCreate(
            type="unknown",
            site_id="public-notes",
            title="An unsupported folio type",
            content="This must remain invalid.",
        )


def test_moment_api_round_trip_and_existing_simple_type_regression(client):
    site = client.post(
        "/skein/sites",
        json={"site_id": "public-notes", "purpose": "Moment regression tests"},
        headers={"X-Agent-Id": "moment-tester"},
    )
    assert site.status_code == 200, site.text

    notion_id = _post_folio(
        client,
        "notion",
        "Explore a quieter release process",
        "Existing notion behavior still works.",
    )
    finding_id = _post_folio(
        client,
        "finding",
        "Release checks caught the stale asset",
        "Existing finding behavior still works.",
    )
    moment_id = _post_folio(
        client,
        "moment",
        "Released the first public build",
        "The beaconword release is ready to be said out loud.",
    )

    assert moment_id.startswith("moment-")
    assert notion_id.startswith("notion-")
    assert finding_id.startswith("finding-")

    listed = client.get("/skein/folios", params={"site_id": "public-notes", "type": "moment"})
    assert listed.status_code == 200
    assert [folio["folio_id"] for folio in listed.json()] == [moment_id]

    activity = client.get("/skein/activity")
    assert activity.status_code == 200
    assert moment_id in {folio["folio_id"] for folio in activity.json()["new_folios"]}

    searched = client.get(
        "/skein/search",
        params={"q": "beaconword", "resources": "folios", "type": "moment"},
    )
    assert searched.status_code == 200
    assert [folio["folio_id"] for folio in searched.json()["results"]["folios"]["items"]] == [
        moment_id
    ]

    read = client.get(f"/skein/folios/{moment_id}")
    assert read.status_code == 200
    assert read.json()["type"] == "moment"

    closed = client.post(
        "/skein/threads",
        json={
            "from_id": moment_id,
            "to_id": moment_id,
            "type": "status",
            "content": "closed",
        },
        headers={"X-Agent-Id": "moment-tester"},
    )
    assert closed.status_code == 200, closed.text
    assert client.get(f"/skein/folios/{moment_id}").json()["status"] == "closed"


def test_post_moment_cli_supports_cross_project_sites_and_details():
    request = MagicMock(return_value={"folio_id": "moment-20260823-test"})

    with patch("client.cli.make_request", request):
        result = CliRunner().invoke(
            cli,
            [
                "post",
                "moment",
                "speakbot:public-notes",
                "Released the first public build",
                "--details",
                "A public marker for the release.",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output
    assert "Posted moment: moment-20260823-test" in result.output
    _, kwargs = request.call_args
    assert kwargs["project_id"] == "speakbot"
    assert kwargs["json"] == {
        "type": "moment",
        "site_id": "public-notes",
        "title": "Released the first public build",
        "content": "A public marker for the release.",
        "metadata": {},
    }


def test_moment_is_advertised_by_cli_and_counted_in_lifecycle():
    post_help = CliRunner().invoke(cli, ["post", "--help"])
    find_help = CliRunner().invoke(cli, ["find", "--help"])

    assert post_help.exit_code == 0
    assert "moment" in post_help.output
    assert "intended for public sharing" in post_help.output
    assert find_help.exit_code == 0
    assert "moment" in find_help.output
    assert FOLIO_SUMMARY_LABELS["moment"] == "moments"
