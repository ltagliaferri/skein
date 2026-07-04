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

import pytest
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
    app.dependency_overrides[R.get_project_log_db] = lambda: db
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
        ccli.cli, ["publish", "myref", "--to", "http://s:9101", "--dry-run"])
    assert result.exit_code == 0, result.output
    posts = [c for c in calls if c[0] == "POST"]
    assert posts and posts[0][1] == "/publish"                       # hits the route
    assert posts[0][2]["manifest"]["folios"] == ["sha256::" + "a" * 64]  # resolved ref
    assert posts[0][2]["dry_run"] is True


def test_dangling_thread_warns_but_still_publishes(monkeypatch):
    ha, va = _mk_version("A")
    hc, _ = _mk_version("C")  # NOT declared -> the edge dangles
    th, tr = _mk_thread(ha, hc)
    db = _FakeDB({ha: va}, {th: tr})
    client, calls = _client(db, monkeypatch)
    r = client.post("/publish", json={"to": "http://s:9101",
                                      "manifest": {"folios": [ha], "threads": [th]},
                                      "token": "t"})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True  # dangling never blocks
    assert any(w["code"] == "dangling" for w in body["warnings"])
