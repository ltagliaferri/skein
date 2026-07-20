"""Phase 4 route contract — POST /publish (docs/PHASE_4_DESIGN.md §4, §9 D/F).

Exercises the curlable route end-to-end with a fake store + an INJECTED signer and a
stubbed ingress POST — no real Sigstore, no network. Covers: dry-run signs/sends
nothing, no-token refusal, the happy path attaches the manifest and posts it, physics
rejects a body that doesn't reproduce its hash (4xx not 500), unknown hashes 404, and
the linter warns-but-doesn't-block.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from skein import publish as P
from skein import routes as R
from skein.identity import compute_folio_hash, compute_thread_hash

# a datetime (NOT a string) — mirrors what the real store returns, so these tests
# exercise the wire created_at serialization the string-fixture tests missed.
CREATED_AT = datetime(2026, 7, 4, 0, 0, 0, tzinfo=timezone.utc)


def _mk_version(title, content_hash=None):
    fields = {"type": "finding", "title": title, "content": "body",
              "created_at": CREATED_AT, "created_by": "agent-x"}
    h = content_hash or compute_folio_hash(fields)  # override -> a physics mismatch
    return h, SimpleNamespace(content_hash=h, type="finding", title=title,
                              content="body", created_at=CREATED_AT, created_by="agent-x")


def _mk_thread(frm, to, ttype="reference"):
    th = compute_thread_hash(frm, to, ttype, None, CREATED_AT, None)
    return th, {"thread_hash": th, "from_id": frm, "to_id": to, "type": ttype,
                "weaver": None, "created_at": CREATED_AT, "content": None}


class _FakeBundle:
    def model_dump_json(self):
        return '{"fake_bundle": true}'


class _FakeSigner:
    def __call__(self, canonical_bytes):
        return P.SignedResult(bundle=_FakeBundle(), issuer="iss@x", subject="sub")


class _FakeDB:
    def __init__(self, versions, threads):
        self._v, self._t = versions, threads

    def get_version_by_hash(self, h):
        return self._v.get(h)

    def get_thread_by_hash(self, th):
        return self._t.get(th)


def _client(db, monkeypatch):
    calls = {"signer": 0, "posted": None}

    def fake_signer_from_token(token, issuer=None):
        calls["signer"] += 1
        return _FakeSigner()

    def fake_post_batch(url, batch):
        json.dumps(batch)  # mirror real post_batch: a non-serializable field (datetime) raises here
        calls["posted"] = batch
        return {"accepted": [], "has_sig": "manifest_signature" in batch, "url": url}

    monkeypatch.setattr(P, "signer_from_token", fake_signer_from_token)
    monkeypatch.setattr(P, "post_batch", fake_post_batch)
    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[R.get_project_store] = lambda: db
    return TestClient(app), calls


def _two_linked_folios():
    ha, va = _mk_version("A")
    hb, vb = _mk_version("B")
    th, tr = _mk_thread(ha, hb)  # reference A->B, both declared
    return _FakeDB({ha: va, hb: vb}, {th: tr}), ha, hb, th


def test_dry_run_signs_and_sends_nothing(monkeypatch):
    db, ha, hb, th = _two_linked_folios()
    client, calls = _client(db, monkeypatch)
    r = client.post("/publish", json={"to": "http://station:9101",
                                      "manifest": {"folios": [ha, hb], "threads": [th]},
                                      "dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["signed"] is False and body["sent"] is False
    assert set(body["declared"]["folios"]) == {ha, hb}
    assert body["declared"]["threads"] == [th]
    assert calls["signer"] == 0 and calls["posted"] is None  # no ceremony, no POST


def test_dry_run_physics_mismatch_is_400(monkeypatch):
    # a dry_run preview must agree with the real send on the physics floor: a stored
    # row whose bytes don't reproduce its hash is a 400 in BOTH, not a clean preview
    # that only fails later on the real send (round-2 fell finding 1)
    bogus = "sha256::" + "0" * 64
    _, bad = _mk_version("A", content_hash=bogus)
    db = _FakeDB({bogus: bad}, {})
    client, calls = _client(db, monkeypatch)
    r = client.post("/publish", json={"to": "http://station:9101",
                                      "manifest": {"folios": [bogus], "threads": []},
                                      "dry_run": True})
    assert r.status_code == 400
    assert "physics" in r.json()["detail"].lower()
    assert calls["signer"] == 0 and calls["posted"] is None  # still signs/sends nothing


def test_real_publish_refused_without_token(monkeypatch):
    db, ha, hb, th = _two_linked_folios()
    client, _ = _client(db, monkeypatch)
    r = client.post("/publish", json={"to": "http://station:9101",
                                      "manifest": {"folios": [ha], "threads": []}})
    assert r.status_code == 400
    assert "token" in r.json()["detail"].lower()


def test_happy_publish_attaches_manifest_and_posts(monkeypatch):
    db, ha, hb, th = _two_linked_folios()
    client, calls = _client(db, monkeypatch)
    r = client.post("/publish", json={"to": "http://station:9101",
                                      "manifest": {"folios": [ha, hb], "threads": [th]},
                                      "token": "oidc-xyz"})
    assert r.status_code == 200
    body = r.json()
    assert body["signed"] is True and body["sent"] is True
    assert calls["signer"] == 1
    # the batch that went to the ingress carried the signature (un-forgettable)
    assert calls["posted"]["manifest_signature"]["descriptor"]["leaf_count"] == 3
    assert body["ack"]["has_sig"] is True


def test_physics_mismatch_is_4xx_not_500(monkeypatch):
    # a stored version whose fields do NOT reproduce the requested hash
    bogus = "sha256::" + "0" * 64
    _, bad = _mk_version("A", content_hash=bogus)
    db = _FakeDB({bogus: bad}, {})
    client, _ = _client(db, monkeypatch)
    r = client.post("/publish", json={"to": "http://station:9101",
                                      "manifest": {"folios": [bogus], "threads": []},
                                      "token": "oidc-xyz"})
    assert r.status_code == 400
    assert "physics" in r.json()["detail"].lower()


def test_unknown_folio_hash_is_404(monkeypatch):
    db = _FakeDB({}, {})
    client, _ = _client(db, monkeypatch)
    r = client.post("/publish", json={"to": "http://s:9101",
                                      "manifest": {"folios": ["sha256::" + "a" * 64],
                                                   "threads": []},
                                      "token": "t"})
    assert r.status_code == 404


def test_empty_manifest_is_400(monkeypatch):
    client, calls = _client(_FakeDB({}, {}), monkeypatch)
    r = client.post("/publish", json={"to": "http://s:9101",
                                      "manifest": {"folios": [], "threads": []}, "token": "t"})
    assert r.status_code == 400 and "empty" in r.json()["detail"].lower()
    assert calls["signer"] == 0  # rejected before any ceremony


def test_schemeless_target_is_400_before_signing(monkeypatch):
    db, ha, hb, th = _two_linked_folios()
    client, calls = _client(db, monkeypatch)
    r = client.post("/publish", json={"to": "ingress.example.com",  # no http(s)://
                                      "manifest": {"folios": [ha], "threads": []}, "token": "t"})
    assert r.status_code == 400
    assert calls["signer"] == 0  # rejected BEFORE the irreversible ceremony


def test_hostless_target_is_400_before_signing(monkeypatch):
    db, ha, hb, th = _two_linked_folios()
    client, calls = _client(db, monkeypatch)
    r = client.post("/publish", json={"to": "http://",  # scheme ok, but NO host
                                      "manifest": {"folios": [ha], "threads": []}, "token": "t"})
    assert r.status_code == 400
    assert calls["signer"] == 0  # rejected before the ceremony (no wasted Rekor)


def test_cli_publish_is_thin_and_delegates_to_the_route(monkeypatch):
    # §9 F2: the CLI resolves refs via the API and POSTs /publish — it never assembles
    # or signs locally (thin client).
    from click.testing import CliRunner
    from client import cli as ccli

    calls = []

    def fake_make_request(method, endpoint, base_url, agent_id, **kwargs):
        calls.append((method, endpoint, kwargs.get("json")))
        if method == "GET":
            return {"content_hash": "sha256::" + "a" * 64}
        return {"declared": {"folios": ["sha256::" + "a" * 64], "threads": []},
                "warnings": [], "signed": False, "sent": False}

    monkeypatch.setattr(ccli, "make_request", fake_make_request)
    result = CliRunner().invoke(
        ccli.cli, ["publish", "myref", "--to", "http://s:9101", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    posts = [c for c in calls if c[0] == "POST"]
    assert posts and posts[0][1] == "/publish"  # hits the route
    assert posts[0][2]["manifest"]["folios"] == ["sha256::" + "a" * 64]  # resolved ref
    assert posts[0][2]["dry_run"] is True


def test_cli_site_publish_is_thin_and_needs_no_ref_lookup(monkeypatch):
    from click.testing import CliRunner
    from client import cli as ccli

    calls = []

    def fake_make_request(method, endpoint, base_url, agent_id, **kwargs):
        calls.append((method, endpoint, kwargs.get("json")))
        assert method == "POST"  # no positional refs: the server selects the site
        return {
            "declared": {
                "folios": ["sha256::" + "a" * 64],
                "threads": [],
                "site_slugs": {"sha256::" + "a" * 64: "public-gnomon"},
            },
            "warnings": [],
            "signed": False,
            "sent": False,
        }

    monkeypatch.setattr(ccli, "make_request", fake_make_request)
    result = CliRunner().invoke(
        ccli.cli,
        [
            "publish",
            "--site",
            "gnomon",
            "--slug",
            "public-gnomon",
            "--to",
            "http://s:9101",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1] == "/publish"
    assert calls[0][2]["site"] == "gnomon"
    assert calls[0][2]["site_slug"] == "public-gnomon"
    assert calls[0][2]["manifest"] == {"folios": [], "threads": []}
    assert "site claim: /site/public-gnomon" in result.output


def test_cli_slug_requires_site_before_login(monkeypatch):
    from click.testing import CliRunner
    from client import cli as ccli
    from skein import publish as P

    ceremonies = []
    monkeypatch.setattr(
        P,
        "acquire_login_token",
        lambda **kwargs: ceremonies.append(1) or {"token": "T", "issuer": "i", "subject": "s"},
    )
    result = CliRunner().invoke(
        ccli.cli,
        ["publish", "--slug", "gnomon", "--to", "http://s:9101", "--login"],
    )
    assert result.exit_code != 0
    assert "--slug requires --site" in result.output
    assert ceremonies == []


def test_cli_login_runs_the_ceremony_and_hands_the_token_to_the_route(monkeypatch):
    # piece-1 (§6): --login runs the interactive ceremony CLIENT-side and passes the
    # resulting token in the request body. The client still assembles/signs nothing.
    from click.testing import CliRunner
    from client import cli as ccli
    from skein import publish as P

    monkeypatch.setattr(
        P,
        "acquire_login_token",
        lambda force_oob=False: {"token": "TOK", "issuer": "iss", "subject": "me@x"},
    )

    posts = []

    def fake_make_request(method, endpoint, base_url, agent_id, **kwargs):
        if method == "GET":
            return {"content_hash": "sha256::" + "a" * 64}
        posts.append((endpoint, kwargs.get("json")))
        return {"declared": {"folios": ["sha256::" + "a" * 64], "threads": []},
                "warnings": [], "signed": True, "sent": True, "ack": {}}

    monkeypatch.setattr(ccli, "make_request", fake_make_request)
    result = CliRunner().invoke(
        ccli.cli, ["publish", "myref", "--to", "http://s:9101", "--login"])
    assert result.exit_code == 0, result.output
    assert posts and posts[0][0] == "/publish"
    assert posts[0][1]["token"] == "TOK"        # the acquired token rode to the route


def test_cli_login_and_token_are_mutually_exclusive():
    from click.testing import CliRunner
    from client import cli as ccli
    result = CliRunner().invoke(
        ccli.cli, ["publish", "myref", "--to", "http://s:9101", "--login", "--token", "T"])
    assert result.exit_code != 0
    assert "one of --login or --token" in result.output


def test_cli_oob_requires_login():
    from click.testing import CliRunner
    from client import cli as ccli
    result = CliRunner().invoke(
        ccli.cli, ["publish", "myref", "--to", "http://s:9101", "--token", "T", "--oob"])
    assert result.exit_code != 0
    assert "--oob only applies with --login" in result.output


def test_cli_real_send_without_identity_is_a_friendly_error():
    # no --login, no --token, not a dry run -> fail fast client-side (the route also 400s).
    from click.testing import CliRunner
    from client import cli as ccli
    result = CliRunner().invoke(ccli.cli, ["publish", "myref", "--to", "http://s:9101"])
    assert result.exit_code != 0
    assert "needs an identity" in result.output


def test_cli_dry_run_skips_the_login_ceremony(monkeypatch):
    # a dry run signs/sends nothing, so --login must NOT pop a browser (ceremony skipped).
    from click.testing import CliRunner
    from client import cli as ccli
    from skein import publish as P

    called = {"n": 0}

    def _should_not_run(force_oob=False):
        called["n"] += 1
        return {"token": "TOK", "issuer": "i", "subject": "s"}

    monkeypatch.setattr(P, "acquire_login_token", _should_not_run)

    def fake_make_request(method, endpoint, base_url, agent_id, **kwargs):
        if method == "GET":
            return {"content_hash": "sha256::" + "a" * 64}
        return {"declared": {"folios": [], "threads": []}, "warnings": [],
                "signed": False, "sent": False}

    monkeypatch.setattr(ccli, "make_request", fake_make_request)
    result = CliRunner().invoke(
        ccli.cli, ["publish", "myref", "--to", "http://s:9101", "--login", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert called["n"] == 0    # ceremony skipped on dry-run


def test_cli_login_failure_is_a_clean_error_not_a_traceback(monkeypatch):
    # a cancelled/failed OIDC ceremony becomes a clean ClickException, and NO publish is
    # attempted (token is never set, so control never reaches the POST).
    from click.testing import CliRunner
    from client import cli as ccli
    from skein import publish as P

    def _boom(force_oob=False):
        raise RuntimeError("user cancelled the browser prompt")

    monkeypatch.setattr(P, "acquire_login_token", _boom)

    calls = []

    def fake_make_request(method, endpoint, base_url, agent_id, **kwargs):
        calls.append(method)
        return {}

    monkeypatch.setattr(ccli, "make_request", fake_make_request)
    result = CliRunner().invoke(
        ccli.cli, ["publish", "myref", "--to", "http://s:9101", "--login"])
    assert result.exit_code != 0
    assert "Sigstore login failed" in result.output   # clean error, not a raw traceback
    assert "RuntimeError" not in result.output         # the traceback did not leak
    assert "POST" not in calls                          # nothing published on a failed login


def test_declared_set_over_max_leaves_is_400_before_any_resolution(monkeypatch):
    # a hostile caller can't force unbounded DB resolution + linting before the
    # MAX_LEAVES/token gates by just naming a huge declared set (fell finding 9).
    resolved = {"n": 0}

    class _CountingDB(_FakeDB):
        def get_version_by_hash(self, h):
            resolved["n"] += 1
            return super().get_version_by_hash(h)

        def get_thread_by_hash(self, th):
            resolved["n"] += 1
            return super().get_thread_by_hash(th)

    ha, va = _mk_version("A")
    db = _CountingDB({ha: va}, {})
    client, calls = _client(db, monkeypatch)
    huge = [ha] * (P.MAX_LEAVES + 1)
    r = client.post(
        "/publish", json={"to": "http://station:9101", "manifest": {"folios": huge, "threads": []}}
    )
    assert r.status_code == 400
    assert "MAX_LEAVES" in r.json()["detail"]
    assert resolved["n"] == 0  # rejected before any get_version_by_hash/get_thread_by_hash
    assert calls["signer"] == 0  # and before the token/ceremony gate


def test_site_slug_claim_does_not_consume_a_merkle_leaf(monkeypatch):
    fields = {
        "type": "site",
        "title": "Site",
        "content": "body",
        "created_at": CREATED_AT,
        "created_by": "agent-x",
    }
    site_hash = compute_folio_hash(fields)
    site = SimpleNamespace(content_hash=site_hash, **fields)
    db = _FakeDB({site_hash: site}, {})
    client, calls = _client(db, monkeypatch)

    response = client.post(
        "/publish",
        json={
            "to": "http://station:9101",
            "manifest": {"folios": [site_hash] * P.MAX_LEAVES, "threads": []},
            "site_slugs": {site_hash: "gnomon"},
            "dry_run": True,
        },
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["declared"]["folios"]) == P.MAX_LEAVES
    assert response.json()["declared"]["site_slugs"] == {site_hash: "gnomon"}
    assert calls["signer"] == 0


def test_dangling_thread_warns_but_still_publishes(monkeypatch):
    ha, va = _mk_version("A")
    hc, _ = _mk_version("C")  # NOT declared -> the edge dangles
    th, tr = _mk_thread(ha, hc)
    db = _FakeDB({ha: va}, {th: tr})
    client, calls = _client(db, monkeypatch)
    r = client.post(
        "/publish",
        json={"to": "http://s:9101", "manifest": {"folios": [ha], "threads": [th]}, "token": "t"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True  # dangling never blocks
    assert any(w["code"] == "dangling" for w in body["warnings"])
