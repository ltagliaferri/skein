"""First-class workbench-site publication: route, persistence, and idempotency."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from skein import publish as P
from skein import routes as R
from skein.identity import compute_thread_hash
from skein.models import Folio, Site, Thread
from skein.storage import JSONStore
from tests import station_publish_helpers as h

CREATED_AT = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _workbench(tmp_path):
    data_dir = tmp_path / "workbench"
    data_dir.mkdir()
    store = JSONStore(data_dir)
    store.save_site(
        Site(
            site_id="gnomon",
            created_at=CREATED_AT,
            created_by="pat",
            purpose="Public Gnomon notes",
        )
    )
    store.save_site(
        Site(
            site_id="private",
            created_at=CREATED_AT,
            created_by="pat",
            purpose="Unrelated private notes",
        )
    )
    members = []
    for folio_id, folio_type, title, created_at in (
        ("finding-gnomon-a", "finding", "Gnomon finding A", CREATED_AT),
        (
            "brief-gnomon-b",
            "brief",
            "Gnomon brief B",
            CREATED_AT.replace(minute=1),
        ),
    ):
        folio = Folio(
            folio_id=folio_id,
            type=folio_type,
            site_id="gnomon",
            created_at=created_at,
            created_by="pat",
            title=title,
            content=f"body for {title}",
        )
        store.save_folio(folio)
        members.append(folio)
    unrelated = Folio(
        folio_id="finding-private",
        type="finding",
        site_id="private",
        created_at=CREATED_AT,
        created_by="pat",
        title="Unrelated private finding",
        content="must never travel with gnomon",
    )
    store.save_folio(unrelated)
    return store, members, unrelated


def _client(store, monkeypatch):
    calls = {"signer": 0, "posted": []}

    def signer_from_token(token):
        calls["signer"] += 1
        return h.make_signer()

    def post_batch(url, batch):
        json.dumps(batch)
        calls["posted"].append(batch)
        return {
            "accepted": [f["content_hash"] for f in batch["folios"]],
            "existing": [],
            "rejected": [],
            "threads": {
                "accepted": [t["thread_hash"] for t in batch["threads"]],
                "existing": [],
                "rejected": [],
                "dangling": [],
            },
        }

    monkeypatch.setattr(P, "signer_from_token", signer_from_token)
    monkeypatch.setattr(P, "post_batch", post_batch)
    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[R.get_project_store] = lambda: store
    return TestClient(app), calls


def test_site_dry_run_is_exact_no_write_and_real_send_is_idempotent(tmp_path, monkeypatch):
    store, members, unrelated = _workbench(tmp_path)
    client, calls = _client(store, monkeypatch)
    before_folios = len(store.get_folios())
    before_threads = len(store.get_threads())

    preview = client.post(
        "/publish",
        json={"to": "https://ingress.example", "site": "gnomon", "dry_run": True},
    )
    assert preview.status_code == 200, preview.text
    declared = preview.json()["declared"]
    anchor = P.build_site_anchor("gnomon", "Public Gnomon notes", CREATED_AT, "pat")
    expected_memberships = [
        P.build_within_membership(f.content_hash, anchor["content_hash"], f.created_at)
        for f in sorted(members, key=lambda item: item.content_hash)
    ]

    assert declared == {
        "folios": [anchor["content_hash"]] + sorted(f.content_hash for f in members),
        "threads": [t["thread_hash"] for t in expected_memberships],
        "site_slugs": {anchor["content_hash"]: "gnomon"},
    }
    assert unrelated.content_hash not in declared["folios"]
    assert calls == {"signer": 0, "posted": []}
    assert len(store.get_folios()) == before_folios
    assert len(store.get_threads()) == before_threads

    sent = client.post(
        "/publish",
        json={"to": "https://ingress.example", "site": "gnomon", "token": "token"},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["declared"] == declared
    assert calls["signer"] == 1
    assert [f["content_hash"] for f in calls["posted"][0]["folios"]] == declared["folios"]
    assert [t["thread_hash"] for t in calls["posted"][0]["threads"]] == declared["threads"]
    assert calls["posted"][0]["site_slugs"] == declared["site_slugs"]
    assert len(store.get_folios()) == before_folios + 1
    assert len(store.get_threads()) == before_threads + len(members)

    # The persisted anchor and within rows converge: another send has the same
    # declaration and does not create new local identities.
    sent_again = client.post(
        "/publish",
        json={"to": "https://ingress.example", "site": "gnomon", "token": "token"},
    )
    assert sent_again.status_code == 200, sent_again.text
    assert sent_again.json()["declared"] == declared
    assert len(store.get_folios()) == before_folios + 1
    assert len(store.get_threads()) == before_threads + len(members)


def test_site_explicit_subset_is_exact_and_cross_site_hash_is_rejected(tmp_path, monkeypatch):
    store, members, unrelated = _workbench(tmp_path)
    client, calls = _client(store, monkeypatch)

    preview = client.post(
        "/publish",
        json={
            "to": "https://ingress.example",
            "site": "gnomon",
            "manifest": {"folios": [members[0].content_hash], "threads": []},
            "dry_run": True,
        },
    )
    assert preview.status_code == 200, preview.text
    assert members[0].content_hash in preview.json()["declared"]["folios"]
    assert members[1].content_hash not in preview.json()["declared"]["folios"]
    assert len(preview.json()["declared"]["threads"]) == 1

    rejected = client.post(
        "/publish",
        json={
            "to": "https://ingress.example",
            "site": "gnomon",
            "manifest": {"folios": [unrelated.content_hash], "threads": []},
            "token": "token",
        },
    )
    assert rejected.status_code == 400
    assert "not a current non-site head" in rejected.json()["detail"]
    assert calls["signer"] == 0 and calls["posted"] == []


def test_site_explicit_threads_must_stay_inside_selected_site(tmp_path, monkeypatch):
    store, members, unrelated = _workbench(tmp_path)
    client, calls = _client(store, monkeypatch)
    inside = Thread(
        thread_id="reference-gnomon",
        from_id=members[0].content_hash,
        to_id=members[1].content_hash,
        type="reference",
        content=None,
        weaver="pat",
        created_at=CREATED_AT,
    )
    foreign = Thread(
        thread_id="reference-private",
        from_id=unrelated.content_hash,
        to_id=unrelated.content_hash,
        type="reference",
        content=None,
        weaver="pat",
        created_at=CREATED_AT,
    )
    assert store.save_thread(inside)
    assert store.save_thread(foreign)
    inside_hash = compute_thread_hash(
        inside.from_id, inside.to_id, inside.type, inside.weaver, inside.created_at, inside.content
    )
    foreign_hash = compute_thread_hash(
        foreign.from_id,
        foreign.to_id,
        foreign.type,
        foreign.weaver,
        foreign.created_at,
        foreign.content,
    )

    accepted = client.post(
        "/publish",
        json={
            "to": "https://ingress.example",
            "site": "gnomon",
            "manifest": {"threads": [inside_hash]},
            "dry_run": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert inside_hash in accepted.json()["declared"]["threads"]

    rejected = client.post(
        "/publish",
        json={
            "to": "https://ingress.example",
            "site": "gnomon",
            "manifest": {"threads": [foreign_hash]},
            "token": "token",
        },
    )
    assert rejected.status_code == 400
    assert "outside the selected workbench site" in rejected.json()["detail"]
    assert calls == {"signer": 0, "posted": []}


def test_remote_failure_leaves_stable_site_state_for_retry(tmp_path, monkeypatch):
    store, members, _ = _workbench(tmp_path)
    client, calls = _client(store, monkeypatch)
    before = (len(store.get_folios()), len(store.get_threads()))

    def fail_post(url, batch):
        raise P.PublishError("remote unavailable")

    monkeypatch.setattr(P, "post_batch", fail_post)
    failed = client.post(
        "/publish",
        json={"to": "https://ingress.example", "site": "gnomon", "token": "token"},
    )
    assert failed.status_code == 502
    after_failure = (len(store.get_folios()), len(store.get_threads()))
    assert after_failure == (before[0] + 1, before[1] + len(members))

    def succeed_post(url, batch):
        calls["posted"].append(batch)
        return {"accepted": [], "existing": [], "rejected": [], "threads": {}}

    monkeypatch.setattr(P, "post_batch", succeed_post)
    retried = client.post(
        "/publish",
        json={"to": "https://ingress.example", "site": "gnomon", "token": "token"},
    )
    assert retried.status_code == 200, retried.text
    assert (len(store.get_folios()), len(store.get_threads())) == after_failure
    assert calls["posted"][0]["site_slugs"] == retried.json()["declared"]["site_slugs"]


def test_explicit_manifest_carries_validated_site_slugs(tmp_path, monkeypatch):
    store, _, _ = _workbench(tmp_path)
    anchor = P.build_site_anchor("gnomon", "Public Gnomon notes", CREATED_AT, "pat")
    store.save_folio(
        Folio(
            folio_id=anchor["folio_id"],
            type="site",
            site_id="gnomon",
            created_at=CREATED_AT,
            created_by="pat",
            title=anchor["title"],
            content=anchor["content"],
        )
    )
    client, calls = _client(store, monkeypatch)
    response = client.post(
        "/publish",
        json={
            "to": "https://ingress.example",
            "manifest": {"folios": [anchor["content_hash"]], "threads": []},
            "site_slugs": {anchor["content_hash"]: "public-gnomon"},
            "token": "token",
        },
    )
    assert response.status_code == 200, response.text
    assert calls["posted"][0]["site_slugs"] == {anchor["content_hash"]: "public-gnomon"}


def test_malformed_site_claims_fail_before_write_login_or_send(tmp_path, monkeypatch):
    store, members, _ = _workbench(tmp_path)
    client, calls = _client(store, monkeypatch)
    before = (len(store.get_folios()), len(store.get_threads()))

    bad_slug = client.post(
        "/publish",
        json={
            "to": "https://ingress.example",
            "site": "gnomon",
            "site_slug": "Bad/Slug",
            "dry_run": True,
        },
    )
    assert bad_slug.status_code == 400

    no_identity = client.post(
        "/publish",
        json={"to": "https://ingress.example", "site": "gnomon"},
    )
    assert no_identity.status_code == 400
    assert "token" in no_identity.json()["detail"]

    non_site_claim = client.post(
        "/publish",
        json={
            "to": "https://ingress.example",
            "manifest": {"folios": [members[0].content_hash], "threads": []},
            "site_slugs": {members[0].content_hash: "gnomon"},
            "token": "token",
        },
    )
    assert non_site_claim.status_code == 400
    assert "not a declared type=site" in non_site_claim.json()["detail"]
    assert calls == {"signer": 0, "posted": []}
    assert (len(store.get_folios()), len(store.get_threads())) == before
