"""Permission model — document_grants store: issue / revoke / bulk-revoke / audit
(rev 6 §3.2, brief-20260716-u73x). Grants are live-revocable, rotation-proof, kind- and
anchor-scoped, and do NOT cascade on granter revocation (the explicit containment verb
does that)."""

from __future__ import annotations

import pytest

from skein.station_store import StationStore

I = "https://accounts.google.com"
ANCHOR = "sha256::" + "a" * 64
ANCHOR2 = "sha256::" + "b" * 64
GRANTEE = (I, "bob@example.com")
ADMIN = (I, "admin@example.com")
ADMIN2 = (I, "admin2@example.com")


@pytest.fixture
def store(tmp_path):
    s = StationStore(tmp_path / ".skein-station")
    yield s
    s.close()


def test_add_then_active(store):
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)
    assert store.has_active_grant(ANCHOR, *GRANTEE, "supersede") is True


def test_absent_grant_inactive(store):
    assert store.has_active_grant(ANCHOR, *GRANTEE, "supersede") is False


def test_kind_scoped(store):
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)
    assert store.has_active_grant(ANCHOR, *GRANTEE, "site_contribute") is False


def test_anchor_scoped(store):
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)
    assert store.has_active_grant(ANCHOR2, *GRANTEE, "supersede") is False


def test_revoke(store):
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)
    assert store.revoke_grant(ANCHOR, *GRANTEE, "supersede") is True
    assert store.has_active_grant(ANCHOR, *GRANTEE, "supersede") is False
    # revoking again finds nothing active
    assert store.revoke_grant(ANCHOR, *GRANTEE, "supersede") is False


def test_reactivate_after_revoke(store):
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)
    store.revoke_grant(ANCHOR, *GRANTEE, "supersede")
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)  # re-grant
    assert store.has_active_grant(ANCHOR, *GRANTEE, "supersede") is True
    events = [e["event"] for e in store.get_grant_events(*GRANTEE)]
    assert events == ["granted", "revoked", "regranted"]


def test_add_active_idempotent_no_event(store):
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)  # already active
    events = [e["event"] for e in store.get_grant_events(*GRANTEE)]
    assert events == ["granted"]


def test_revoke_grants_vouched_by_is_containment(store):
    """The containment verb revokes ALL active grants vouched by a granter, leaving
    another granter's grants untouched (grants do NOT auto-cascade, §3.2)."""
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)
    store.add_grant(ANCHOR2, *GRANTEE, "site_contribute", *ADMIN)
    store.add_grant(ANCHOR, *GRANTEE, "site_edit", *ADMIN2)  # a DIFFERENT granter
    n = store.revoke_grants_vouched_by(*ADMIN)
    assert n == 2
    assert store.has_active_grant(ANCHOR, *GRANTEE, "supersede") is False
    assert store.has_active_grant(ANCHOR2, *GRANTEE, "site_contribute") is False
    # ADMIN2's grant survives — no cascade
    assert store.has_active_grant(ANCHOR, *GRANTEE, "site_edit") is True


def test_list_grants_active_vs_all(store):
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)
    store.add_grant(ANCHOR2, *GRANTEE, "site_contribute", *ADMIN)
    store.revoke_grant(ANCHOR2, *GRANTEE, "site_contribute")
    assert len(store.list_grants()) == 1
    assert len(store.list_grants(include_revoked=True)) == 2


def test_grant_events_append_only_monotonic(store):
    store.add_grant(ANCHOR, *GRANTEE, "supersede", *ADMIN)
    store.revoke_grant(ANCHOR, *GRANTEE, "supersede")
    seqs = [e["event_seq"] for e in store.get_grant_events()]
    assert seqs == sorted(seqs)


def test_grant_kind_check_rejects_unknown(store):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.add_grant(ANCHOR, *GRANTEE, "not_a_kind", *ADMIN)
