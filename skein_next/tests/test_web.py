"""Tests for the slice-3 web read surface: HTML rendered FROM the wire envelope,
the stationfile wiring, and the theming substrate.

The legacy ContentHashAdapter is retired; HTML and the machine wire are now built
from the one envelope, so the interesting properties to pin are: the two surfaces
agree (no divergence), the page is content-first in the DOM, the stationfile
drives identity + theme (with fail-loud on an unnamed station), and the token ->
CSS-variable path reaches the page.
"""

import json

import pytest
from fastapi.testclient import TestClient

from skein_next.station import Station
from skein_next.stationfile import StationfileError
from skein_next.web.app import (
    ENV_DATA_DIR,
    ENV_PROJECT,
    create_app,
    verdict_state,
)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / ".skein-next"


@pytest.fixture
def seeded(data_dir):
    """A station with two linked folios, a status thread, and a sub-site."""
    with Station(data_dir) as st:
        st.create_site("proj", purpose="the project")
        a = st.post(type="finding", site="proj", title="Finding A", content="body A here",
                    created_by="alice", created_at="2026-01-01T00:00:00Z")
        b = st.post(type="brief", site="proj", title="Brief B", content="body B here",
                    created_by="bob", created_at="2026-01-02T00:00:00Z")
        st.store.save_thread(from_id=a, to_id=b, type="reference",
                             created_at="2026-01-03T00:00:00Z")
        st.store.save_thread(to_id=b, type="status", content="closed",
                             created_at="2026-01-04T00:00:00Z")
    return {"data_dir": data_dir, "a": a, "b": b}


def _write_stationfile(data_dir, obj):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stationfile.json").write_text(json.dumps(obj), encoding="utf-8")


def _make_client(data_dir, monkeypatch, *, stationfile=None, env_project=None):
    monkeypatch.setenv(ENV_DATA_DIR, str(data_dir))
    if env_project is None:
        monkeypatch.delenv(ENV_PROJECT, raising=False)
    else:
        monkeypatch.setenv(ENV_PROJECT, env_project)
    if stationfile is not None:
        _write_stationfile(data_dir, stationfile)
    return TestClient(create_app())


@pytest.fixture
def client(seeded, monkeypatch):
    return _make_client(seeded["data_dir"], monkeypatch, stationfile={"name": "Field Notes"})


# --- stationfile wiring / fail-loud -----------------------------------------


def test_unnamed_station_refuses_to_start(seeded, monkeypatch):
    # No stationfile and no SKEIN_NEXT_PROJECT bootstrap -> create_app fails loud.
    monkeypatch.setenv(ENV_DATA_DIR, str(seeded["data_dir"]))
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    with pytest.raises(StationfileError):
        create_app()


def test_env_project_bootstraps_name(seeded, monkeypatch):
    client = _make_client(seeded["data_dir"], monkeypatch, env_project="interskein")
    r = client.get("/")
    assert r.status_code == 200 and "interskein" in r.text


def test_stationfile_name_wins_over_env(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "Field Notes"}, env_project="interskein",
    )
    r = client.get("/")
    assert "Field Notes" in r.text and "interskein" not in r.text


def test_tagline_renders(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "Field Notes", "tagline": "notes from the mesh"},
    )
    assert "notes from the mesh" in client.get("/").text


# --- index / site / search HTML ---------------------------------------------


def test_index_lists_sites_and_recent(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "proj" in r.text
    assert "Finding A" in r.text and "Brief B" in r.text


def test_site_detail_and_type_filter(client):
    r = client.get("/site/proj")
    assert r.status_code == 200 and "Finding A" in r.text and "Brief B" in r.text
    r = client.get("/site/proj", params={"type": "finding"})
    assert "Finding A" in r.text and "Brief B" not in r.text


def test_site_404_is_themed_error(client):
    r = client.get("/site/ghost")
    assert r.status_code == 404
    assert "Not resolved" in r.text  # the themed error page, not a bare JSON detail


def test_search_route(client):
    r = client.get("/search", params={"q": "body"})
    assert r.status_code == 200
    assert "Finding A" in r.text and "Brief B" in r.text
    r = client.get("/search", params={"q": "no-such-text-anywhere"})
    assert r.status_code == 200 and "No matches" in r.text


# --- folio HTML from the envelope -------------------------------------------


def test_folio_html_from_wire(client, seeded):
    r = client.get(f"/folio/{seeded['a']}")
    assert r.status_code == 200
    assert "Finding A" in r.text          # title from env.body.title
    assert "body A here" in r.text        # rendered markdown body
    assert seeded["a"] in r.text          # the content hash (provenance)
    assert "UNSIGNED" in r.text           # the verdict line
    assert "provenance--unsigned" in r.text
    assert "Brief B" in r.text            # threads_out peer title


def test_folio_threads_in_and_out(client, seeded):
    # b is referenced BY a — the incoming edge must surface as "Referenced by".
    r = client.get(f"/folio/{seeded['b']}")
    assert "Referenced by" in r.text
    assert "Finding A" in r.text


def test_folio_content_first_source_order(client, seeded):
    # Patrick screen-reader hard req: the folio body precedes the provenance /
    # threads chrome in the DOM.
    r = client.get(f"/folio/{seeded['a']}").text
    body_at = r.index("folio-body")
    aside_at = r.index("folio-meta")
    refs_at = r.index("References")
    assert body_at < aside_at < refs_at


def test_folio_404_is_themed_error(client):
    # A well-formed full digest that resolves to nothing -> not_found, themed.
    r = client.get("/folio/sha256::" + "0" * 64)
    assert r.status_code == 404 and "Not resolved" in r.text


def test_html_and_json_agree_on_status(client, seeded):
    # The whole point of HTML-from-wire: no divergence. b is closed via a status
    # thread; both surfaces must report it (HTML reads the same asserted block).
    html = client.get(f"/folio/{seeded['b']}").text
    env = client.get(f"/folio/{seeded['b']}.json").json()
    assert env["asserted"]["status"] == "closed"
    assert "closed" in html


# --- theming substrate ------------------------------------------------------


def test_base_and_default_theme_linked(client):
    r = client.get("/").text
    assert "/static/base.css" in r
    assert "/static/themes/ulm.css" in r  # ulm is the default


def test_classic_theme_selected(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "theme": "classic"},
    )
    r = client.get("/").text
    assert "/static/themes/classic.css" in r
    assert "/static/themes/ulm.css" not in r


def test_token_accent_reaches_the_page(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "tokens": {"accent": "#123456"}},
    )
    r = client.get("/").text
    assert "--accent: #123456;" in r


def test_font_stack_token_not_html_escaped(seeded, monkeypatch):
    # A quoted font name must reach the <style> block as literal CSS, not
    # &#39;-escaped (which would break font-family). Safe because the loader
    # already stripped the markup-breaking chars.
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "tokens": {"font_body": "Georgia, 'Times New Roman', serif"}},
    )
    r = client.get("/").text
    assert "--font-body: Georgia, 'Times New Roman', serif;" in r


def test_default_theme_token_sets_data_theme(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "tokens": {"default_theme": "dark"}},
    )
    assert 'data-theme="dark"' in client.get("/").text


def test_shipped_theme_static_served(client):
    r = client.get("/static/themes/ulm.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


def test_custom_theme_served_from_data_dir(seeded, monkeypatch):
    themes = seeded["data_dir"] / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "mine.css").write_text("body { color: rebeccapurple; }", encoding="utf-8")
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "theme": "themes/mine.css"},
    )
    page = client.get("/").text
    assert "/theme.css" in page
    css = client.get("/theme.css")
    assert css.status_code == 200 and "rebeccapurple" in css.text


# --- negotiation still routes (the wire is unchanged) -----------------------


def test_machine_wire_still_negotiates(client, seeded):
    # An agent UA / Accept still gets the wire, not HTML.
    r = client.get(f"/folio/{seeded['a']}", headers={"accept": "application/json"})
    assert r.json()["kind"] == "folio"
    r = client.get(f"/folio/{seeded['a']}.md")
    assert "body A here" in r.text


# --- unit -------------------------------------------------------------------


def test_verdict_state_mapping():
    assert verdict_state("SIGNED — alice (verified)") == "verified"
    assert verdict_state("SIGNATURE INVALID — bad sig") == "invalid"
    assert verdict_state("UNVERIFIED — verifier unavailable (X)") == "unverified"
    assert verdict_state("UNSIGNED — operator-vouched") == "unsigned"
    assert verdict_state(None) == "unsigned"


def test_concurrent_requests_isolated_connections(client):
    import concurrent.futures

    def hit(_):
        return client.get("/").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        codes = list(ex.map(hit, range(40)))
    assert codes == [200] * 40
