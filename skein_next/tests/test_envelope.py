"""Tests for the unified wire envelope (skein_next.envelope)."""

from __future__ import annotations

import pytest

from skein import signing
from skein_next import envelope as env_mod
from skein_next.station import Station


@pytest.fixture
def seeded(tmp_path):
    """Two linked folios in a site, a status thread, plus a cross-instance edge."""
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
        st.store.save_thread(
            to_id=b, type="status", content="closed", created_at="2026-01-04T00:00:00Z"
        )
        # An edge to a peer that has no local folio (cross-instance).
        st.store.save_thread(
            from_id=a, to_id="sha256::" + "f" * 64, type="cites", created_at="2026-01-05T00:00:00Z"
        )
    store = SkeinNextStoreRO(data_dir)
    yield {"store": store.store, "a": a, "b": b}
    store.close()


class SkeinNextStoreRO:
    """A throwaway store handle. Writable so the signed-verdict tests can attach a
    bundle sidecar; envelope construction itself only ever reads."""

    def __init__(self, data_dir):
        from skein_next.store import SkeinNextStore

        self.store = SkeinNextStore(data_dir, check_same_thread=False)

    def close(self):
        self.store.close()


# --- validate_envelope ------------------------------------------------------


def test_validate_rejects_unknown_kind():
    with pytest.raises(ValueError):
        env_mod.validate_envelope({"kind": "nonsense", "stability": "stable"})


def test_validate_enforces_kind_stability():
    with pytest.raises(ValueError):
        env_mod.validate_envelope({"kind": "folio", "stability": "derived", "proof": {}})
    with pytest.raises(ValueError):
        env_mod.validate_envelope({"kind": "search", "stability": "stable"})


def test_validate_stable_needs_proof():
    with pytest.raises(ValueError):
        env_mod.validate_envelope({"kind": "folio", "stability": "stable", "proof": None})


def test_validate_derived_needs_as_of_and_no_proof():
    with pytest.raises(ValueError):
        env_mod.validate_envelope({"kind": "search", "stability": "derived", "proof": {"x": 1}})
    with pytest.raises(ValueError):
        env_mod.validate_envelope(
            {"kind": "search", "stability": "derived", "proof": None, "as_of": None}
        )


# --- build_folio_envelope ---------------------------------------------------


def test_folio_envelope_shape(seeded):
    env = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    assert env["schema"] == env_mod.SCHEMA
    assert env["kind"] == "folio" and env["stability"] == "stable"
    assert env["as_of"] is None
    assert env["body"] == {
        "type": "finding",
        "title": "Finding A",
        "content": "# A\n\nbody A",
        "created_at": "2026-01-01T00:00:00+00:00",
        "created_by": "alice",
    }
    assert env["proof"]["profile"] == env_mod.CANON_PROFILE
    assert env["proof"]["content_hash"] == seeded["a"]
    assert env["proof"]["signature_bundle"] is None  # unsigned -> integrity level
    assert env["links"]["self"] == f"/folio/{seeded['a']}"
    assert "bundle" not in env["links"]  # no bundle link when unsigned


def test_folio_asserted_status_and_site(seeded):
    env_a = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    env_b = env_mod.build_folio_envelope(seeded["store"], seeded["b"])
    assert env_a["asserted"]["status"] == "open"
    assert env_b["asserted"]["status"] == "closed"  # thread-derived
    assert env_a["asserted"]["site"]["slug"] == "proj"
    assert env_a["asserted"]["site"]["href"] == "/site/proj"


def test_folio_threads_split_by_direction(seeded):
    env_a = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    env_b = env_mod.build_folio_envelope(seeded["store"], seeded["b"])
    # a -> b reference is outgoing for a, incoming for b
    out_types = {(t["type"], t["address"]) for t in env_a["asserted"]["threads_out"]}
    assert ("reference", seeded["b"]) in out_types
    in_b = {(t["type"], t["address"]) for t in env_b["asserted"]["threads_in"]}
    assert ("reference", seeded["a"]) in in_b


def test_folio_threads_exclude_structural(seeded):
    env_b = env_mod.build_folio_envelope(seeded["store"], seeded["b"])
    # the status edge and the within (membership) edge must NOT appear as threads
    all_types = {
        t["type"] for t in env_b["asserted"]["threads_out"] + env_b["asserted"]["threads_in"]
    }
    assert "status" not in all_types and "within" not in all_types


def test_folio_cross_instance_peer_listed_with_raw_address(seeded):
    env_a = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    cites = [t for t in env_a["asserted"]["threads_out"] if t["type"] == "cites"]
    assert len(cites) == 1
    assert cites[0]["address"] == "sha256::" + "f" * 64
    assert cites[0]["title"] is None  # not held locally, no title


def test_folio_threads_exclude_alias_self_loop(seeded):
    # An edge to a legacy id that aliases back to THIS folio must not list the
    # folio as its own neighbour (the direct-hash self-loop is already excluded;
    # this is the alias-to-self case).
    store = seeded["store"]
    store.set_alias("finding-20260101-self", seeded["a"])
    store.save_thread(
        from_id=seeded["a"],
        to_id="finding-20260101-self",
        type="relates",
        created_at="2026-01-09T00:00:00Z",
    )
    env_a = env_mod.build_folio_envelope(store, seeded["a"])
    addresses = {t["address"] for t in env_a["asserted"]["threads_out"]}
    assert seeded["a"] not in addresses
    assert "finding-20260101-self" not in addresses


# --- folio_verdict ----------------------------------------------------------


def test_verdict_unsigned(seeded):
    verdict, identity = env_mod.folio_verdict(
        seeded["store"], seeded["a"], seeded["store"].get_folio(seeded["a"])
    )
    assert verdict.startswith("UNSIGNED")
    assert identity is None


def _store_fake_bundle(store, content_hash):
    row = store.get_folio(content_hash)
    from skein_next import canon

    bundle = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["x"],
        canonical_bytes=canon.folio_canonical_bytes(row),
    )
    store.set_signature(content_hash, bundle.model_dump_json())


def _verify_result(status, **kw):
    return signing.MultiVerifyResult(
        results=[signing.VerifyResult(status=status, **kw)], overall=status
    )


def test_verdict_signed(seeded, monkeypatch):
    _store_fake_bundle(seeded["store"], seeded["a"])
    monkeypatch.setattr(
        signing,
        "verify_multi",
        lambda cb, b: _verify_result(
            signing.VerifyStatus.VERIFIED, issuer="iss", subject="alice@example.com"
        ),
    )
    verdict, identity = env_mod.folio_verdict(
        seeded["store"], seeded["a"], seeded["store"].get_folio(seeded["a"])
    )
    assert verdict == "SIGNED — alice@example.com (verified)"
    assert identity["subject"] == "alice@example.com"


def test_verdict_unverifiable_is_not_invalid(seeded, monkeypatch):
    _store_fake_bundle(seeded["store"], seeded["a"])
    monkeypatch.setattr(
        signing,
        "verify_multi",
        lambda cb, b: _verify_result(signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT),
    )
    verdict, _ = env_mod.folio_verdict(
        seeded["store"], seeded["a"], seeded["store"].get_folio(seeded["a"])
    )
    assert verdict.startswith("UNVERIFIED")
    assert "INVALID" not in verdict


def test_verdict_invalid(seeded, monkeypatch):
    _store_fake_bundle(seeded["store"], seeded["a"])
    monkeypatch.setattr(
        signing,
        "verify_multi",
        lambda cb, b: _verify_result(signing.VerifyStatus.SIGNATURE_MISMATCH),
    )
    verdict, _ = env_mod.folio_verdict(
        seeded["store"], seeded["a"], seeded["store"].get_folio(seeded["a"])
    )
    assert verdict.startswith("SIGNATURE INVALID")


def test_signed_folio_envelope_carries_bundle_and_link(seeded, monkeypatch):
    _store_fake_bundle(seeded["store"], seeded["a"])
    monkeypatch.setattr(
        signing,
        "verify_multi",
        lambda cb, b: _verify_result(signing.VerifyStatus.VERIFIED, issuer="iss", subject="sub"),
    )
    env = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    assert env["proof"]["signature_bundle"] is not None  # integrity + authorship
    assert env["proof"]["signature_bundle"]["identity_scheme"] == "sigstore-public-v1"
    assert env["links"]["bundle"] == f"/folio/{seeded['a']}/bundle"


# --- collection / error -----------------------------------------------------


def test_collection_envelope_is_derived():
    env = env_mod.build_collection_envelope("search", "/search?q=x", [])
    assert env["stability"] == "derived"
    assert env["proof"] is None
    assert env["as_of"]  # stamped


def test_error_envelope():
    env = env_mod.build_error_envelope(
        "not_found", "sha256::" + "0" * 64, origin="web::x.example::sha256::" + "0" * 64
    )
    assert env["kind"] == "error" and env["body"]["error"] == "not_found"
    assert env["links"]["origin"].startswith("web::")
    assert env["suggestion"]
