"""Route-level tests for the machine wire: content negotiation, the envelope over
HTTP, caching headers, structured errors, and the bundle sub-resource."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skein import signing
from skein_next import canon
from skein_next.station import Station
from skein_next.web.app import ENV_DATA_DIR, ENV_PROJECT, create_app, negotiate


@pytest.fixture
def seeded(tmp_path):
    data_dir = tmp_path / ".skein-next"
    with Station(data_dir) as st:
        st.create_site("proj", purpose="the project")
        a = st.post(
            type="finding",
            site="proj",
            title="Finding A",
            content="# A\n\nbody A",
            created_by="alice",
            created_at="2026-01-01T00:00:00Z",
        )
        b = st.post(
            type="brief",
            site="proj",
            title="Brief B",
            content="body B",
            created_by="bob",
            created_at="2026-01-02T00:00:00Z",
        )
        st.store.save_thread(
            from_id=a, to_id=b, type="reference", created_at="2026-01-03T00:00:00Z"
        )
    return {"data_dir": data_dir, "a": a, "b": b}


@pytest.fixture
def client(seeded, monkeypatch):
    monkeypatch.setenv(ENV_DATA_DIR, str(seeded["data_dir"]))
    monkeypatch.setenv(ENV_PROJECT, "interskein")  # name the station (the wire is name-agnostic)
    return TestClient(create_app())


# --- negotiate (unit) -------------------------------------------------------


@pytest.mark.parametrize(
    "suffix,accept,ua,expected",
    [
        ("json", None, None, "json"),
        ("md", None, None, "md_raw"),
        (None, "application/json", None, "json"),
        (None, "text/markdown", None, "markdown"),
        (None, "text/html", "Mozilla/5.0", "html"),
        (None, "*/*", "Mozilla/5.0", "html"),
        (None, "*/*", "curl/8.0", "markdown"),
        (None, "*/*", "skein/0.1", "markdown"),
        (None, "*/*", "testclient", "html"),  # unknown UA -> HTML (human surface)
        (None, None, None, "html"),
        ("json", "text/html", "Mozilla", "json"),  # suffix wins over Accept
    ],
)
def test_negotiate(suffix, accept, ua, expected):
    assert negotiate(suffix, accept, ua) == expected


# --- folio representations --------------------------------------------------


def test_json_folio(client, seeded):
    r = client.get(f"/folio/{seeded['a']}.json")
    assert r.status_code == 200
    env = r.json()
    assert env["kind"] == "folio" and env["proof"]["content_hash"] == seeded["a"]
    assert env["body"]["title"] == "Finding A"
    # The envelope embeds mutable `asserted` state, so it is NOT immutable: it
    # revalidates, and the ETag is over the payload bytes (not the content hash).
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["etag"] != f'"{seeded["a"]}"'


def test_json_folio_conditional_304(client, seeded):
    # A conditional GET 304s only against the *payload* ETag (whole envelope),
    # never against the content hash.
    first = client.get(f"/folio/{seeded['a']}.json")
    etag = first.headers["etag"]
    r = client.get(f"/folio/{seeded['a']}.json", headers={"If-None-Match": etag})
    assert r.status_code == 304
    # The content-hash spelling must NOT satisfy the conditional (that was the bug).
    r2 = client.get(f"/folio/{seeded['a']}.json", headers={"If-None-Match": f'"{seeded["a"]}"'})
    assert r2.status_code == 200


def test_json_folio_etag_tracks_asserted_not_just_hash(seeded, monkeypatch):
    """A status change flips the envelope ETag while the content hash is unchanged
    — proving the asserted block is not pinned by an immutable cache (B1)."""
    monkeypatch.setenv(ENV_DATA_DIR, str(seeded["data_dir"]))
    monkeypatch.setenv(ENV_PROJECT, "interskein")
    before = TestClient(create_app()).get(f"/folio/{seeded['b']}.json")
    assert before.json()["asserted"]["status"] == "open"
    etag_before = before.headers["etag"]

    with Station(seeded["data_dir"]) as st:  # close the folio via a status thread
        st.store.save_thread(
            to_id=seeded["b"],
            type="status",
            content="closed",
            created_at="2026-02-01T00:00:00Z",
        )
    after = TestClient(create_app()).get(f"/folio/{seeded['b']}.json")
    assert after.json()["asserted"]["status"] == "closed"
    assert after.json()["proof"]["content_hash"] == seeded["b"]  # hash unchanged
    assert after.headers["etag"] != etag_before  # but the validator moved


def test_markdown_via_accept(client, seeded):
    r = client.get(f"/folio/{seeded['a']}", headers={"Accept": "text/markdown"})
    assert r.status_code == 200
    assert "x-skein-nonce" in r.headers
    assert r.headers["cache-control"] == "no-store"
    nonce = r.headers["x-skein-nonce"]
    assert f"===={nonce}==" in r.text
    assert "body A" in r.text


def test_markdown_nonce_changes_per_fetch(client, seeded):
    n1 = client.get(f"/folio/{seeded['a']}", headers={"Accept": "text/markdown"}).headers[
        "x-skein-nonce"
    ]
    n2 = client.get(f"/folio/{seeded['a']}", headers={"Accept": "text/markdown"}).headers[
        "x-skein-nonce"
    ]
    assert n1 != n2


def test_raw_md(client, seeded):
    r = client.get(f"/folio/{seeded['a']}.md")
    assert r.status_code == 200
    assert r.text == "# A\n\nbody A\n"
    assert "immutable" in r.headers["cache-control"]


def test_html_still_default_for_browser(client, seeded):
    r = client.get(
        f"/folio/{seeded['a']}", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"}
    )
    assert r.status_code == 200
    assert "<" in r.text  # HTML, not the wire


# --- errors -----------------------------------------------------------------


def test_not_found_json(client):
    r = client.get("/folio/sha256::" + "0" * 64 + ".json")
    assert r.status_code == 404
    assert r.json()["body"] == {"found": False, "error": "not_found"}
    assert r.headers["cache-control"] == "no-store"


def test_invalid_address_json(client):
    r = client.get("/folio/not-an-address.json")
    assert r.status_code == 400
    assert r.json()["body"]["error"] == "invalid_address"


def test_error_markdown(client):
    r = client.get("/folio/sha256::" + "0" * 64, headers={"Accept": "text/markdown"})
    assert r.status_code == 404
    assert "NOT RESOLVED" in r.text


# --- collections ------------------------------------------------------------


def test_catalog_json(client):
    r = client.get("/.json")
    assert r.status_code == 200
    env = r.json()
    assert env["kind"] == "catalog" and env["stability"] == "derived"
    assert env["asserted"]["wire"] == "skein.envelope/v1"
    assert any(s["slug"] == "proj" for s in env["asserted"]["sites"])
    assert r.headers["cache-control"] == "no-cache"


def test_catalog_via_accept(client):
    r = client.get("/", headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["kind"] == "catalog"


def test_site_json(client, seeded):
    r = client.get("/site/proj.json")
    assert r.status_code == 200
    env = r.json()
    assert env["kind"] == "site" and env["asserted"]["count"] == 2
    assert {e["address"] for e in env["body"]} == {seeded["a"], seeded["b"]}


def test_site_json_honors_type_filter(client, seeded):
    # The machine listing must honor ?type= just like the HTML branch — silently
    # ignoring it would be a contract gap (a/finding, b/brief in the fixture).
    r = client.get("/site/proj.json", params={"type": "brief"})
    assert r.status_code == 200
    env = r.json()
    assert env["asserted"]["type"] == "brief" and env["asserted"]["count"] == 1
    assert {e["address"] for e in env["body"]} == {seeded["b"]}


def test_site_json_unknown_is_error(client):
    r = client.get("/site/ghost.json")
    assert r.status_code == 404
    assert r.json()["body"]["error"] == "not_found"


# --- navigation: no dead ends -----------------------------------------------


def test_navigate_corpus_over_the_wire(client):
    """The slice-1 demo: from the catalog alone, every address an agent is handed
    resolves over the wire — no dead ends for local content (§6)."""
    catalog = client.get("/.json").json()

    # Every catalog entry resolves to a folio.
    for entry in catalog["body"]:
        assert client.get(f"/folio/{entry['address']}.json").status_code == 200

    # Every site the catalog names resolves, and so does every member it lists.
    for site in catalog["asserted"]["sites"]:
        listing = client.get(f"{site['href']}.json").json()
        assert listing["kind"] == "site"
        for entry in listing["body"]:
            assert client.get(f"/folio/{entry['address']}.json").status_code == 200

    # Every locally-held thread peer (one carrying a title) resolves too.
    for entry in catalog["body"]:
        folio = client.get(f"/folio/{entry['address']}.json").json()
        edges = folio["asserted"]["threads_out"] + folio["asserted"]["threads_in"]
        for edge in edges:
            if edge["title"] is not None:  # cross-instance peers 404 by design
                assert client.get(f"/folio/{edge['address']}.json").status_code == 200


# --- bundle sub-resource ----------------------------------------------------


def test_bundle_unsigned_is_404(client, seeded):
    r = client.get(f"/folio/{seeded['a']}/bundle")
    assert r.status_code == 404
    assert r.json()["body"]["error"] == "not_found"


def test_bundle_served_when_signed(seeded, monkeypatch):
    # Attach a bundle sidecar, then the bundle route returns it verbatim.
    with Station(seeded["data_dir"]) as st:
        row = st.store.get_folio(seeded["a"])
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=["x"],
            canonical_bytes=canon.folio_canonical_bytes(row),
        )
        st.store.set_signature(seeded["a"], bundle.model_dump_json())
    monkeypatch.setenv(ENV_DATA_DIR, str(seeded["data_dir"]))
    monkeypatch.setenv(ENV_PROJECT, "interskein")
    client = TestClient(create_app())
    r = client.get(f"/folio/{seeded['a']}/bundle")
    assert r.status_code == 200
    assert r.json()["identity_scheme"] == "sigstore-public-v1"
    # The bundle is re-signable for an unchanged hash (slice 2), so it revalidates.
    assert r.headers["cache-control"] == "no-cache"


# --- batch resolve (fork D) -------------------------------------------------


def test_batch_resolve_array_in_order(client, seeded):
    r = client.post("/resolve", json=[seeded["a"], seeded["b"]])
    assert r.status_code == 200
    envs = r.json()
    assert [e["kind"] for e in envs] == ["folio", "folio"]
    # request order preserved; each element is its own verifiable envelope
    assert envs[0]["proof"]["content_hash"] == seeded["a"]
    assert envs[1]["proof"]["content_hash"] == seeded["b"]
    assert r.headers["cache-control"] == "no-store"


def test_batch_resolve_errors_inline(client, seeded):
    r = client.post("/resolve", json=[seeded["a"], "sha256::" + "0" * 64])
    envs = r.json()
    assert envs[0]["kind"] == "folio"
    assert envs[1]["kind"] == "error" and envs[1]["body"]["found"] is False


def test_batch_resolve_empty_is_empty_array(client):
    r = client.post("/resolve", json=[])
    assert r.status_code == 200 and r.json() == []


def test_batch_resolve_over_cap_rejected_whole(client, seeded):
    from skein_next.web.app import BATCH_CAP

    r = client.post("/resolve", json=[seeded["a"]] * (BATCH_CAP + 1))
    assert r.status_code == 413
    assert r.json()["kind"] == "error"
    assert r.json()["body"]["error"] == "batch_too_large"


def test_batch_resolve_at_cap_ok(client, seeded):
    from skein_next.web.app import BATCH_CAP

    r = client.post("/resolve", json=[seeded["a"]] * BATCH_CAP)
    assert r.status_code == 200 and len(r.json()) == BATCH_CAP


def test_batch_resolve_oversized_body_rejected_before_parse(client):
    from skein_next.web.app import MAX_BATCH_BYTES

    # A body over the byte cap is rejected 413 (the DoS guard fires before the
    # element-count cap, which can't run until after a parse).
    big = "x" * (MAX_BATCH_BYTES + 1)
    r = client.post("/resolve", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert r.json()["body"]["error"] == "batch_too_large"


def test_batch_resolve_non_json_body(client):
    r = client.post("/resolve", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["body"]["error"] == "invalid_batch"


def test_batch_resolve_non_array_body(client):
    r = client.post("/resolve", json={"addresses": []})
    assert r.status_code == 400 and r.json()["body"]["error"] == "invalid_batch"


# --- machine search (slice 5) -----------------------------------------------


def test_search_json(client):
    r = client.get("/search.json", params={"q": "body"})
    assert r.status_code == 200
    env = r.json()
    assert env["kind"] == "search" and env["stability"] == "derived"
    assert env["asserted"]["query"] == "body"
    titles = [e["title"] for e in env["body"]]
    assert "Finding A" in titles and "Brief B" in titles


def test_search_json_has_snippets(client):
    env = client.get("/search.json", params={"q": "body"}).json()
    assert all("snippet" in e for e in env["body"])
    assert any(e["snippet"] for e in env["body"])


def test_search_json_truncated_flag(client):
    env = client.get("/search.json", params={"q": "body"}).json()
    # A handful of results is well under the cap -> not truncated.
    assert env["asserted"]["truncated"] is False


def test_search_via_accept(client):
    r = client.get("/search", params={"q": "body"}, headers={"Accept": "application/json"})
    assert r.json()["kind"] == "search"


def test_search_md(client):
    r = client.get("/search.md", params={"q": "body"})
    assert r.status_code == 200 and "Finding A" in r.text


# --- well-known / describe (slice 5) ----------------------------------------


def test_well_known_json(client):
    r = client.get("/.well-known/skein.json")
    assert r.status_code == 200
    doc = r.json()
    assert doc["skein"] == "station/v1"
    assert doc["wire"] == "skein.envelope/v1"
    assert doc["profile"] == "skein.folio.canon/v1"
    assert "resolve" in doc["operations"] and "search" in doc["operations"]
    assert "ignore any delimiter" in doc["nonce_fence"]
    assert doc["totals"]["folios"] >= 1


def test_well_known_negotiates_json_by_default(client):
    # A browser with no explicit preference gets JSON (metadata, not a page).
    r = client.get("/.well-known/skein", headers={"User-Agent": "Mozilla/5.0"})
    assert r.json()["skein"] == "station/v1"


def test_well_known_md(client):
    r = client.get("/.well-known/skein.md")
    assert r.status_code == 200
    assert "SKEIN station" in r.text and "Operations:" in r.text
