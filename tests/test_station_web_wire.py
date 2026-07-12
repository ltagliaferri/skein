"""Route-level tests for the machine wire: content negotiation, the envelope over
HTTP, caching headers, structured errors, and the bundle sub-resource."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skein import signing
from skein.web.app import (
    ENV_BASE_URL,
    ENV_DATA_DIR,
    ENV_NAME,
    create_app,
    negotiate,
    public_base_url,
)

from tests.station_read_helpers import StationBuilder


@pytest.fixture
def seeded(tmp_path):
    data_dir = tmp_path / ".skein-next"
    with StationBuilder(data_dir) as st:
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
    monkeypatch.setenv(ENV_NAME, "interskein")  # name the station (the wire is name-agnostic)
    return TestClient(create_app())


# --- negotiate (unit) -------------------------------------------------------


@pytest.mark.parametrize(
    "suffix,accept,ua,expected",
    [
        ("json", None, None, "json"),
        ("md", None, None, "markdown"),
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


class _FakeRequest:
    def __init__(self, base_url):
        self.base_url = base_url


def test_base_url_prefers_valid_config(monkeypatch):
    monkeypatch.setenv(ENV_BASE_URL, "https://interskein.com/")
    assert public_base_url(_FakeRequest("http://127.0.0.1:9001/")) == "https://interskein.com"


def test_base_url_rejects_schemeless(monkeypatch):
    # A scheme-less value would yield a non-absolute URL; ignore it and fall back.
    monkeypatch.setenv(ENV_BASE_URL, "interskein.com")
    assert public_base_url(_FakeRequest("http://127.0.0.1:9001/")) == "http://127.0.0.1:9001"


def test_public_base_url_request_fallback(monkeypatch):
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    assert public_base_url(_FakeRequest("https://host.test/")) == "https://host.test"


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


def test_folio_etag_tracks_asserted_block(seeded, monkeypatch):
    """A status change flips the envelope ETag while the content hash is unchanged
    — proving the asserted block is not pinned by an immutable cache (B1)."""
    monkeypatch.setenv(ENV_DATA_DIR, str(seeded["data_dir"]))
    monkeypatch.setenv(ENV_NAME, "interskein")
    before = TestClient(create_app()).get(f"/folio/{seeded['b']}.json")
    assert before.json()["asserted"]["status"] == "open"
    etag_before = before.headers["etag"]

    with StationBuilder(seeded["data_dir"]) as st:  # close the folio via a status thread
        # Faithful self-loop status shape (from_id == to_id == folio).
        st.set_status(seeded["b"], "closed", created_at="2026-02-01T00:00:00Z")
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


def test_md_suffix_is_agent_markdown(client, seeded):
    # Decision A (brief-20260606-7ddh): the `.md` URL serves the full agent markdown
    # (opener + fenced body + fetchable references), not raw body. Raw body.content
    # is reached via `.json`. Per-fetch nonce ⇒ no-store, not immutable.
    r = client.get(f"/folio/{seeded['a']}.md")
    assert r.status_code == 200
    assert r.text.startswith("> SKEIN folio: " + seeded["a"])
    assert "x-skein-nonce" in r.headers
    assert r.headers["cache-control"] == "no-store"
    assert "immutable" not in r.headers["cache-control"]
    assert "body A" in r.text  # the body is still there, just framed


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
    # Cover the folio with a manifest; the bundle route returns the covering
    # manifest's bundle verbatim (per-folio bundles no longer exist).
    import json as _json

    from skein import profile, sign as sign_mod
    from skein.canon import manifest_descriptor_canonical_bytes
    from skein.identity import content_hash_for_bytes

    def signer(cb):
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, cb)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer="https://idp", subject="alice")

    with StationBuilder(seeded["data_dir"]) as st:
        ms = sign_mod.sign_manifest([seeded["a"]], signer)
        d = ms["descriptor"]
        mh = content_hash_for_bytes(manifest_descriptor_canonical_bytes(d["root"], d["leaf_count"]))
        with st.store.transaction():
            st.store.add_manifest(d["root"], mh, _json.dumps(d, sort_keys=True),
                                  _json.dumps(ms["leaf_list"]), ms["signature_bundle"],
                                  "https://idp", "alice", d["leaf_count"])
            st.store.add_constituent_attribution(seeded["a"], "folio", d["root"],
                                                 "https://idp", "alice")
    monkeypatch.setenv(ENV_DATA_DIR, str(seeded["data_dir"]))
    monkeypatch.setenv(ENV_NAME, "interskein")
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


def test_batch_resolve_over_cap_rejected(client, seeded):
    from skein.web.app import BATCH_CAP

    r = client.post("/resolve", json=[seeded["a"]] * (BATCH_CAP + 1))
    assert r.status_code == 413
    assert r.json()["kind"] == "error"
    assert r.json()["body"]["error"] == "batch_too_large"


def test_batch_resolve_at_cap_ok(client, seeded):
    from skein.web.app import BATCH_CAP

    r = client.post("/resolve", json=[seeded["a"]] * BATCH_CAP)
    assert r.status_code == 200 and len(r.json()) == BATCH_CAP


def test_batch_resolve_oversized_body_413(client):
    from skein.web.app import MAX_BATCH_BYTES

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


# --- content negotiation: Vary (theming rev 3 §5) ---------------------------


def test_vary_accept_on_negotiated_responses(client, seeded):
    # Every Accept/UA-negotiated representation must carry Vary: Accept so a shared
    # cache keys on it (RFC 9110).
    assert client.get(f"/folio/{seeded['a']}.json").headers.get("vary") == "Accept"
    assert client.get(f"/folio/{seeded['a']}.md").headers.get("vary") == "Accept"
    assert client.get(
        f"/folio/{seeded['a']}", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"}
    ).headers.get("vary") == "Accept"
    assert client.get("/.json").headers.get("vary") == "Accept"
    assert client.get("/.well-known/skein.json").headers.get("vary") == "Accept"


def test_describe_has_html_source_order(client):
    assert client.get("/.well-known/skein.json").json()["html_source_order"] == "content-first"


def test_base_css_dark_mode_by_preference(client):
    # Dark mode is preference-driven with a light fallback; the explicit-theme
    # selector must not catch the no-override case (so OS preference can win).
    css = client.get("/static/base.css").text
    assert "@media (prefers-color-scheme: dark)" in css
    assert ":root:not([data-theme])" in css
    assert ".skip-link" in css and ":focus-visible" in css


def test_default_theme_consumes_dark_tokens(client):
    # The default theme must read the dark-aware tokens (so OS-preference dark
    # restyles code blocks + separators), NOT hardcode colors behind a
    # [data-theme="dark"] element override that an OS-dark page never matches.
    css = client.get("/static/themes/ulm.css").text
    assert "var(--code-bg)" in css and "var(--hairline)" in css
    assert "[data-theme=" not in css  # no element-level explicit-theme-only overrides


def test_bundle_error_omits_vary(client):
    # The bundle subresource is fixed JSON (not Accept-negotiated); its error must
    # not carry Vary: Accept.
    r = client.get("/folio/sha256::" + "0" * 64 + "/bundle")
    assert r.status_code == 404
    assert "vary" not in r.headers


def test_site_alternate_keeps_type_filter(client):
    head = client.get("/site/proj?type=brief").text
    head = head[: head.index("</head>")]
    assert 'href="/site/proj.md?type=brief"' in head
    assert 'href="/site/proj.json?type=brief"' in head


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


def test_well_known_defaults_to_json(client):
    # A browser with no explicit preference gets JSON (metadata, not a page).
    r = client.get("/.well-known/skein", headers={"User-Agent": "Mozilla/5.0"})
    assert r.json()["skein"] == "station/v1"


def test_well_known_md(client):
    r = client.get("/.well-known/skein.md")
    assert r.status_code == 200
    assert "SKEIN station" in r.text and "Operations:" in r.text


# --- post-cutover read-surface hardening (finding-20260710-lx37) -------------


@pytest.mark.parametrize(
    "accept,expected",
    [
        # The old fixed-priority substring scan returned json here; RFC 9110 says
        # the higher-q type wins, so text/html (q=1) beats application/json (q=0.1).
        ("text/html, application/json;q=0.1", "html"),
        ("application/json;q=0.1, text/markdown;q=0.9", "markdown"),
        # q=0 marks a type unacceptable -> skipped, the other acceptable type wins.
        ("application/json;q=0, text/html", "html"),
        # equal q keeps the json > markdown > html tie-break.
        ("application/json, text/html", "json"),
        ("text/markdown, text/html", "markdown"),
        ("text/html;q=0.5, text/markdown;q=0.5, application/json;q=0.5", "json"),
        # a malformed q degrades to 1 (never raises), so the type stays acceptable.
        ("application/json;q=bogus", "json"),
        ("application/json;q=", "json"),
        # media type + q parameter are case-insensitive; whitespace is tolerated.
        ("Application/JSON ; Q=0.2 , TEXT/HTML", "html"),
    ],
)
def test_negotiate_honors_accept_qvalues(accept, expected):
    assert negotiate(None, accept, None) == expected


def test_negotiate_q0_only_falls_through_to_user_agent():
    # A type explicitly refused (q=0) is not selected; with no other acceptable
    # supported type the negotiation falls to the UA branch — curl -> markdown, no
    # UA -> HTML — exactly as an Accept naming none of the three would.
    assert negotiate(None, "application/json;q=0", "curl/8.0") == "markdown"
    assert negotiate(None, "application/json;q=0", None) == "html"


def test_catalog_returns_newest_30_pushed_to_sql(tmp_path, monkeypatch):
    # 35 findings (+ the older site folio = 36 total); the catalog renders only the
    # newest 30 by created_at, read via recent_folios (SQL ORDER BY ... LIMIT), and
    # count_folios still reports the full total.
    data_dir = tmp_path / ".skein-next"
    with StationBuilder(data_dir) as st:
        st.create_site("proj", purpose="p", created_at="2026-01-01T00:00:00Z")
        hashes = []
        for i in range(35):
            ts = f"2026-02-01T00:{i:02d}:00Z"  # minutes 00..34: valid and monotonic
            hashes.append(
                st.post(type="finding", site="proj", title=f"F{i:02d}", content="x", created_at=ts)
            )
    monkeypatch.setenv(ENV_DATA_DIR, str(data_dir))
    monkeypatch.setenv(ENV_NAME, "interskein")
    env = TestClient(create_app()).get("/.json").json()
    body = env["body"]
    assert len(body) == 30  # capped, not the whole 36-folio corpus
    assert env["asserted"]["total_folios"] == 36  # total still counts everything
    # newest-first: the last-created folio leads; the 5 oldest findings + the site are cut.
    assert [e["address"] for e in body] == list(reversed(hashes))[:30]


def test_site_envelope_address_is_urlencoded(client):
    # The envelope address percent-encodes the type value the way the HTML alternate
    # link does (Jinja urlencode == quote(safe="/")): a space and '&' are escaped so
    # the address is a valid URL, not raw ``?type=a b&c``.
    env = client.get("/site/proj.json", params={"type": "a b&c"}).json()
    assert env["address"] == "/site/proj?type=a%20b%26c"


def test_search_envelope_address_is_urlencoded(client):
    env = client.get("/search.json", params={"q": "a b&c"}).json()
    assert env["address"] == "/search?q=a%20b%26c"


def test_search_truncated_is_honest_at_the_boundary(tmp_path, monkeypatch):
    # 3 matching folios; vary the cap. The fix: exactly CAP matches with nothing cut
    # is NOT truncated (the old `>= SEARCH_LIMIT` flagged that boundary falsely).
    data_dir = tmp_path / ".skein-next"
    with StationBuilder(data_dir) as st:
        st.create_site("proj", created_at="2026-01-01T00:00:00Z")
        for i in range(3):
            st.post(type="finding", site="proj", title=f"hit {i}", content="needle",
                    created_at=f"2026-02-01T00:0{i}:00Z")
    monkeypatch.setenv(ENV_DATA_DIR, str(data_dir))
    monkeypatch.setenv(ENV_NAME, "interskein")

    def result():
        env = TestClient(create_app()).get("/search.json", params={"q": "needle"}).json()
        return env["asserted"]["count"], env["asserted"]["truncated"]

    monkeypatch.setattr("skein.web.app.SEARCH_LIMIT", 2)
    assert result() == (2, True)   # 3 matches, cap 2 -> a row was cut -> truncated
    monkeypatch.setattr("skein.web.app.SEARCH_LIMIT", 3)
    assert result() == (3, False)  # exactly CAP matches, nothing cut -> honest False
    monkeypatch.setattr("skein.web.app.SEARCH_LIMIT", 4)
    assert result() == (3, False)  # under the cap


def test_search_truncated_serves_cap_entries_and_never_leaks_the_probe(tmp_path, monkeypatch):
    # The overflow probe returns SEARCH_LIMIT+1 rows to detect truncation, but the served
    # body must stay exactly SEARCH_LIMIT and must NOT include that extra probe row
    # (finding-20260710-lx37 fix #4). Four body-only "needle" matches, distinct created_at
    # so ranking is pure recency: the oldest (00:00) ranks last and is the probe row at cap 3.
    data_dir = tmp_path / ".skein-next"
    hashes = []
    with StationBuilder(data_dir) as st:
        st.create_site("proj", created_at="2026-01-01T00:00:00Z")
        for i in range(4):
            hashes.append(st.post(type="finding", site="proj", title=f"hit {i}",
                                  content="needle body", created_at=f"2026-02-01T00:0{i}:00Z"))
    probe_row = hashes[0]  # oldest created_at -> ranked last -> excluded at cap 3
    monkeypatch.setenv(ENV_DATA_DIR, str(data_dir))
    monkeypatch.setenv(ENV_NAME, "interskein")

    def env():
        return TestClient(create_app()).get("/search.json", params={"q": "needle"}).json()

    # SEARCH_LIMIT+1 (=4) in-window matches at cap 3: truncated, exactly 3 served, probe absent.
    monkeypatch.setattr("skein.web.app.SEARCH_LIMIT", 3)
    e = env()
    assert e["asserted"]["truncated"] is True
    assert e["asserted"]["count"] == 3
    assert len(e["body"]) == 3
    assert probe_row not in [entry["address"] for entry in e["body"]]

    # exactly SEARCH_LIMIT (=4) matches at cap 4: nothing cut -> not truncated, all served.
    monkeypatch.setattr("skein.web.app.SEARCH_LIMIT", 4)
    e = env()
    assert e["asserted"]["truncated"] is False
    assert e["asserted"]["count"] == 4
    assert len(e["body"]) == 4


def test_conditional_if_none_match_list_star_and_weak(client, seeded):
    first = client.get(f"/folio/{seeded['a']}.json")
    etag = first.headers["etag"]  # strong quoted sha256: "<hex>"

    def status_for(inm):
        return client.get(f"/folio/{seeded['a']}.json", headers={"If-None-Match": inm}).status_code

    assert status_for(etag) == 304                      # exact match (regression guard)
    assert status_for(f'"nomatch", {etag}') == 304      # comma list, one member matches
    assert status_for("W/" + etag) == 304               # weak spelling of the same tag
    assert status_for("*") == 304                       # '*' matches any representation
    assert status_for('"aaa", "bbb"') == 200            # a list with no matching member
