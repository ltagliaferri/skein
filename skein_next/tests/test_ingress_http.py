"""Ingress HTTP surface — the public /publish/v0/folios endpoint hardening.

These exercise the route itself (body byte-cap, shape rejects, the async +
threadpool happy path), distinct from test_require_signed.py which drives the
pure ``ingest`` function. The byte cap is the DoS guard against a hostile client
forcing unbounded parse/memory work before any verification runs (defense-in-
depth atop the fronting proxy's client_max_body_size).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from skein_next import wire
from skein_next.ingress import create_app, MAX_BATCH_BYTES, ENV_DATA_DIR
from skein_next.station import Station


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A fresh instance data dir wired to the endpoint via the env (require_signed
    stays OFF — these are surface tests, not gate tests)."""
    d = tmp_path / "instance" / ".skein-next"
    Station(d).close()  # materialize the store
    monkeypatch.setenv(ENV_DATA_DIR, str(d))
    monkeypatch.delenv("SKEIN_NEXT_REQUIRE_SIGNED", raising=False)
    return d


@pytest.fixture
def app_client(data_dir):
    return TestClient(create_app())


def _small_unsigned_batch(tmp_path):
    """A minimal valid unsigned publish batch from a throwaway client station."""
    from skein_next import publish as pub

    c = Station(tmp_path / "src" / ".skein-next")
    try:
        c.create_site("specs", purpose="Public specs", created_by="t")
        c.post("finding", "specs", "Design Overview", "body", created_by="t")
        folios, threads, slugs = pub.collect_publish_set(c, site="specs")
        return wire.build_batch(folios, threads, slugs)
    finally:
        c.close()


def test_oversized_body_rejected_413_before_parse(app_client):
    # A body over the byte cap is rejected WHOLE (413) — the content-length
    # fast-path fires before the body is buffered or JSON-parsed.
    payload = b'{"protocol":"' + b"x" * (MAX_BATCH_BYTES + 1) + b'"}'
    r = app_client.post(
        "/publish/v0/folios", content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert "too large" in r.json()["error"]


def test_body_at_cap_is_not_rejected_for_size(app_client):
    # Exactly at the cap is allowed past the size guard (it then fails on JSON/shape,
    # not 413) — proves the boundary is inclusive and the cap isn't off-by-one.
    payload = b"y" * MAX_BATCH_BYTES  # not valid JSON -> 400, never 413
    r = app_client.post(
        "/publish/v0/folios", content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_non_json_body_rejected_400(app_client):
    r = app_client.post(
        "/publish/v0/folios", content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert "not valid JSON" in r.json()["error"]


def test_non_object_json_rejected_400(app_client):
    r = app_client.post(
        "/publish/v0/folios", content=b"[1, 2, 3]",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert "JSON object" in r.json()["error"]


def test_wrong_protocol_rejected_400(app_client):
    r = app_client.post("/publish/v0/folios", json={"protocol": "bogus/v9"})
    assert r.status_code == 400
    assert "unknown protocol" in r.json()["error"]


def test_valid_unsigned_publish_lands_through_async_route(app_client, tmp_path):
    # The happy path through the converted async + threadpool route: a small
    # unsigned batch is accepted and stored (require_signed OFF).
    batch = _small_unsigned_batch(tmp_path)
    r = app_client.post("/publish/v0/folios", json=batch)
    assert r.status_code == 200
    ack = r.json()
    assert ack["protocol"] == wire.PROTOCOL
    # the site + finding folios are newly accepted
    assert len(ack["accepted"]) >= 2
    assert ack["rejected"] == []


async def test_client_disconnect_midbody_is_handled_quietly(data_dir):
    # A client that drops mid-body raises starlette ClientDisconnect inside the
    # body-read loop. The route must catch it and return cleanly (no propagating
    # exception, no ASGI traceback) — an adversary-controllable disconnect must
    # not be a logged application error (log-amplification DoS). Driven directly
    # against the endpoint with an ASGI receive that sends http.disconnect mid-stream.
    from starlette.requests import ClientDisconnect, Request

    app = create_app()
    endpoint = next(
        r.endpoint for r in app.routes
        if getattr(r, "path", None) == "/publish/v0/folios"
    )

    messages = [
        {"type": "http.request", "body": b'{"protocol":', "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0)

    scope = {
        "type": "http", "method": "POST", "path": "/publish/v0/folios",
        "headers": [(b"content-type", b"application/json")], "query_string": b"",
    }
    request = Request(scope, receive)

    try:
        resp = await endpoint(request)  # must NOT raise ClientDisconnect
    except ClientDisconnect:  # pragma: no cover - the bug this guards against
        pytest.fail("ClientDisconnect leaked out of the route as an application error")
    assert resp.status_code == 400
