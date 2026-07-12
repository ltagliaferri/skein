"""Thread E — the unified require_signed decision table (RS1-RS20), the accepted
replay stances (C40/C41/C42/C44), and the ingress totality (VM11/VM12).

Re-homed from skein_next/tests/test_require_signed.py (station re-home Stage 3), driving
the re-homed ``skein.ingress.ingest`` over a ``StationStore``. Client publish content is
built directly (``tests/station_publish_helpers``) — the skein_next authoring verbs are
DROP. ONE table over CONSTITUENTS (folios AND threads judged identically by membership).
OFF is manifest-blind AND binding-blind (byte-identical to pre-mesh); ON admits a
constituent iff it is a leaf under a manifest that verifies AND whose signer is bound +
non-revoked, gating the binding ONCE per manifest.
"""

from __future__ import annotations

import copy

import pytest

from skein import wire
from skein import sign as sign_mod
from skein.station import Station
from skein.ingress import ingest, _validate_shape, create_app

from tests import station_publish_helpers as h
from tests.station_publish_helpers import I, ALICE


# --- verifier fakes ---------------------------------------------------------

_ok_verifier = h.ok_verifier
_bad_verifier = h.bad_verifier


class SpyBindings:
    def __init__(self, store):
        self._store = store
        self.get_binding_calls = 0

    def get_binding(self, issuer, subject):
        self.get_binding_calls += 1
        return self._store.get_binding(issuer, subject)


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein-next")
    yield s
    s.close()


def _unsigned_batch():
    folios, threads, slugs = h.specs_set()
    return wire.build_batch(folios, threads, slugs)


def _signed_batch(signer=None, addresses_override=None):
    """A publish batch with a manifest_signature over its constituents."""
    folios, threads, slugs = h.specs_set()
    batch = wire.build_batch(folios, threads, slugs)
    batch["manifest_signature"] = h.manifest_over(
        folios, threads, signer=signer, addresses_override=addresses_override
    )
    return batch


def _site_hash(batch):
    return next(f["content_hash"] for f in batch["folios"] if f["type"] == "site")


def _finding_hash(batch):
    return next(f["content_hash"] for f in batch["folios"] if f["type"] == "finding")


def _bind(instance, issuer=I, subject=ALICE, role="author"):
    instance.store.add_binding(issuer, subject, role=role,
                               vouched_by_issuer=issuer, vouched_by_subject=subject)


# --- RS1-RS12: the unified decision table -----------------------------------


def test_off_unsigned_constituent_accepts(instance):  # RS1
    ack = ingest(instance, _unsigned_batch(), require_signed=False)
    assert len(ack["accepted"]) == 2 and ack["rejected"] == []


def test_on_no_manifest_rejects(instance):  # RS2
    ack = ingest(instance, _unsigned_batch(), require_signed=True)
    assert ack["accepted"] == []
    assert all(r["reason"] == "no manifest" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_manifest_bad_signature_rejects(instance):  # RS3
    _bind(instance)
    ack = ingest(instance, _signed_batch(), verifier=_bad_verifier, require_signed=True)
    assert ack["accepted"] == []
    assert all(r["reason"] == "manifest signature SIGNATURE_MISMATCH" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_manifest_signer_unbound_rejects(instance):  # RS4
    ack = ingest(instance, _signed_batch(), verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "unbound signer" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_manifest_signer_revoked_rejects(instance):  # RS5
    _bind(instance)
    instance.store.revoke_binding(I, ALICE)
    ack = ingest(instance, _signed_batch(), verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "revoked binding" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_leaf_not_in_manifest_rejects(instance):  # RS6
    _bind(instance)
    folios, threads, slugs = h.specs_set()
    batch = wire.build_batch(folios, threads, slugs)
    # manifest covers ONLY the site folio, not the finding
    site_hash = next(f["content_hash"] for f in batch["folios"] if f["type"] == "site")
    batch["manifest_signature"] = sign_mod.sign_manifest([site_hash], h.make_signer())
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    reasons = {r["content_hash"]: r["reason"] for r in ack["rejected"]}
    finding = next(f["content_hash"] for f in batch["folios"] if f["type"] == "finding")
    assert reasons[finding] == "not in manifest"
    # SECURITY GUARD (the accept-and-flag relaxation must NOT let a non-member dangling
    # thread slip in): the within thread here is BOTH a non-member AND dangling; it must
    # still reject 'not in manifest', not be accepted/flagged.
    assert ack["threads"]["rejected"]
    assert all(r["reason"] == "not in manifest" for r in ack["threads"]["rejected"])
    assert not ack["threads"]["accepted"]
    assert not ack["threads"]["dangling"]


def test_on_covered_bound_accepts(instance):  # RS7
    _bind(instance)
    ack = ingest(instance, _signed_batch(), verifier=_ok_verifier, require_signed=True)
    assert len(ack["accepted"]) == 2
    for h_ in ack["accepted"]:
        proof = instance.store.get_constituent_proof(h_)
        assert proof["subject"] == ALICE and proof["proof_missing"] is False


def test_on_manifest_binding_gated_once(instance):  # RS8
    _bind(instance)
    spy = SpyBindings(instance.store)
    ingest(instance, _signed_batch(), verifier=_ok_verifier, require_signed=True, bindings=spy)
    assert spy.get_binding_calls == 1


def test_on_missing_bindings_table_raises(instance):
    """Deploy-fix #1: a SCHEMA FAULT on the ingress authz gate must surface, not be
    masked as an ordinary rejection. The read-path tolerance (a missing-table
    OperationalError -> None) is scoped to read_only stores; the ingress store is
    read_write, where a missing account_bindings table is a genuine fault."""
    import sqlite3

    _bind(instance)
    batch = _signed_batch()
    assert instance.store.read_only is False
    instance.store.conn.execute("DROP TABLE account_bindings")
    instance.store.conn.commit()

    with pytest.raises(sqlite3.OperationalError) as ei:
        ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert "no such table" in str(ei.value).lower()


def test_off_ignores_bindings_and_manifest(instance):  # RS9
    batch = _signed_batch()  # carries a manifest, but OFF must ignore it
    spy = SpyBindings(instance.store)
    ack = ingest(instance, batch, verifier=_bad_verifier, require_signed=False, bindings=spy)
    assert spy.get_binding_calls == 0
    assert len(ack["accepted"]) == 2 and ack["rejected"] == []


def test_reject_reasons_pairwise_distinct():  # RS10
    from skein.ingress import _constituent_manifest_reject_reason as R
    authz = {"no manifest", "manifest signature SIGNATURE_MISMATCH",
             "unbound signer", "revoked binding", "not in manifest"}
    wire_integrity = {"hash mismatch", "invalid fields"}
    manifest_wire = {"manifest malformed", "wrong kind", "unknown profile"}
    assert R("no manifest") == "no manifest"
    assert R("manifest malformed") == "manifest malformed"  # bare, NOT wrapped
    assert R("wrong kind") == "wrong kind"
    assert R("unknown profile") == "unknown profile"
    assert R("SIGNATURE_MISMATCH") == "manifest signature SIGNATURE_MISMATCH"
    all_reasons = authz | wire_integrity | manifest_wire
    assert len(all_reasons) == 10  # all pairwise distinct


def test_attribution_is_manifest_signer(instance):  # RS11
    _bind(instance)
    # the finding is authored by 'mallory' but attribution must be the manifest signer
    folios, threads, slugs = h.specs_set(finding_created_by="mallory")
    batch = wire.build_batch(folios, threads, slugs)
    batch["manifest_signature"] = h.manifest_over(folios, threads)
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    finding = _finding_hash(batch)
    proof = instance.store.get_constituent_proof(finding)
    assert proof["issuer"] == I and proof["subject"] == ALICE  # NOT 'mallory'


def test_default_bindings_is_station_store(instance):  # RS12
    _bind(instance)
    ack = ingest(instance, _signed_batch(), verifier=_ok_verifier, require_signed=True)  # no bindings=
    assert len(ack["accepted"]) == 2


# --- RS13-RS20: isolation, idempotency, OFF->ON attribution, slug gate -------


def test_per_item_savepoint_isolation(instance):  # RS13
    _bind(instance)
    batch = _signed_batch()
    # malform ONE folio body so it rejects, sibling still commits; HTTP-equivalent 200
    batch["folios"][0]["content_hash"] = "sha256::" + "0" * 64  # hash mismatch
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert len(ack["rejected"]) >= 1
    assert len(ack["accepted"]) >= 1


def test_manifest_replay_cannot_inject_leaf(instance):  # RS14
    _bind(instance)
    batch1 = _signed_batch()
    ingest(instance, batch1, verifier=_ok_verifier, require_signed=True)
    # a NEW folio not in the old manifest, replayed under the OLD manifest_signature
    folios, threads, slugs = h.specs_set(finding_title="New", finding_content="newbody")
    batch2 = wire.build_batch(folios, threads, slugs)
    batch2["manifest_signature"] = batch1["manifest_signature"]  # OLD manifest
    l2 = _finding_hash(batch2)
    ack = ingest(instance, batch2, verifier=_ok_verifier, require_signed=True)
    rejected = {r["content_hash"]: r["reason"] for r in ack["rejected"]}
    assert rejected.get(l2) == "not in manifest"


def test_same_manifest_idempotent_republish(instance):  # RS15
    _bind(instance)
    batch = _signed_batch()
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    again = ingest(instance, copy.deepcopy(batch), verifier=_ok_verifier, require_signed=True)
    assert again["accepted"] == []
    assert len(again["existing"]) == 2
    root = batch["manifest_signature"]["descriptor"]["root"]
    assert len(instance.store.get_manifest_proofs_by_root(root)) == 1


def test_mixed_and_single_kind_batches_gated(instance):  # RS16
    _bind(instance)
    batch = _signed_batch()  # site folio + finding + the 'within' thread
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert len(ack["accepted"]) == 2
    assert len(ack["threads"]["accepted"]) == 1  # the within edge is a leaf too
    # a thread-only batch with NO manifest under ON -> 'no manifest'
    folios, threads, slugs = h.specs_set()
    thread_only = {"protocol": wire.PROTOCOL, "folios": [], "threads": wire.build_batch([], threads)["threads"],
                   "site_slugs": {}}
    ack2 = ingest(instance, thread_only, require_signed=True)
    assert all(r["reason"] == "no manifest" for r in ack2["threads"]["rejected"])


def test_slug_cross_author_last_write_wins(instance):  # RS19
    """v0 policy: a second bound author overwriting a slug is last-write-wins."""
    _bind(instance)
    ingest(instance, _signed_batch(), verifier=_ok_verifier, require_signed=True)
    first_site = instance.store.resolve_slug("specs")
    # a second bound author publishes a DIFFERENT site folio under the same slug
    folios, threads, slugs = h.specs_set(site_title="DIFFERENT")
    batch = wire.build_batch(folios, threads, slugs)
    batch["manifest_signature"] = h.manifest_over(folios, threads)
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    s2 = _site_hash(batch)
    assert instance.store.resolve_slug("specs") == s2  # last write wins
    assert s2 != first_site


def test_ingress_no_binding_mutation_route(monkeypatch):  # RS17
    monkeypatch.delenv("SKEIN_NEXT_REQUIRE_SIGNED", raising=False)
    app = create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any("account" in p or "binding" in p for p in paths)


def test_off_then_on_body_gains_attribution(instance):  # RS20
    # 1) ingest the finding under OFF (no manifest, no attribution)
    off_batch = _unsigned_batch()
    ingest(instance, off_batch, require_signed=False)
    finding = _finding_hash(off_batch)
    assert instance.store.get_constituent_proof(finding) is None  # UNSIGNED
    # 2) re-deliver under ON inside a valid bound covering manifest
    _bind(instance)
    on_batch = _signed_batch()
    ack = ingest(instance, on_batch, verifier=_ok_verifier, require_signed=True)
    assert finding in ack["existing"]  # body de-dups
    proof = instance.store.get_constituent_proof(finding)
    assert proof is not None and proof["subject"] == ALICE


def test_off_then_on_unverified_unattributed(instance):  # RS20 negative
    off_batch = _unsigned_batch()
    ingest(instance, off_batch, require_signed=False)
    finding = _finding_hash(off_batch)
    _bind(instance)
    on_batch = _signed_batch()
    ingest(instance, on_batch, verifier=_bad_verifier, require_signed=True)  # crypto fails
    assert instance.store.get_constituent_proof(finding) is None  # stays UNSIGNED


def test_on_slug_gated_by_site_membership(instance):  # RS18
    _bind(instance)
    folios, threads, slugs = h.specs_set()
    batch = wire.build_batch(folios, threads, slugs)
    finding = next(f["content_hash"] for f in batch["folios"] if f["type"] == "finding")
    # manifest covers ONLY the finding, NOT the site folio -> site folio rejected
    batch["manifest_signature"] = sign_mod.sign_manifest([finding], h.make_signer())
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert instance.store.resolve_slug("specs") is None  # no slug landed


# --- C40-C44: accepted replay stances ---------------------------------------


def test_subset_replay_own_manifest_accepted(instance):  # C41
    _bind(instance)
    folios, threads, slugs = h.specs_set()
    full = wire.build_batch(folios, threads, slugs)
    ms = h.manifest_over(folios, threads)
    site = next(f for f in full["folios"] if f["type"] == "site")
    finding = next(f for f in full["folios"] if f["type"] == "finding")
    # publish 1 delivers only the site folio (subset)
    b1 = {"protocol": wire.PROTOCOL, "folios": [site], "threads": [], "site_slugs": slugs,
          "manifest_signature": ms}
    ingest(instance, b1, verifier=_ok_verifier, require_signed=True)
    # later replay the SAME manifest delivering the finding -> accepts
    b2 = {"protocol": wire.PROTOCOL, "folios": [finding], "threads": [], "site_slugs": {},
          "manifest_signature": ms}
    ack = ingest(instance, b2, verifier=_ok_verifier, require_signed=True)
    assert finding["content_hash"] in ack["accepted"]


def test_revoke_between_publishes_regates(instance):  # C40
    _bind(instance)
    folios, threads, slugs = h.specs_set()
    full = wire.build_batch(folios, threads, slugs)
    ms = h.manifest_over(folios, threads)
    site = next(f for f in full["folios"] if f["type"] == "site")
    finding = next(f for f in full["folios"] if f["type"] == "finding")
    b1 = {"protocol": wire.PROTOCOL, "folios": [site], "threads": [], "site_slugs": slugs,
          "manifest_signature": ms}
    ingest(instance, b1, verifier=_ok_verifier, require_signed=True)
    instance.store.revoke_binding(I, ALICE)
    b2 = {"protocol": wire.PROTOCOL, "folios": [finding], "threads": [], "site_slugs": {},
          "manifest_signature": ms}
    ack = ingest(instance, b2, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "revoked binding" for r in ack["rejected"])  # re-gated live


def test_cross_instance_replay_contained(tmp_path):  # C42
    """A batch accepted on station A replays to station B as 'unbound signer' --
    bindings are per-instance."""
    batch = _signed_batch()
    a = Station(tmp_path / "A" / ".skein-next")
    a.store.add_binding(I, ALICE, role="author")
    ackA = ingest(a, copy.deepcopy(batch), verifier=_ok_verifier, require_signed=True)
    assert len(ackA["accepted"]) == 2
    a.close()
    b = Station(tmp_path / "B" / ".skein-next")
    ackB = ingest(b, copy.deepcopy(batch), verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "unbound signer" for r in ackB["rejected"])
    b.close()


def test_replay_accepted_after_rebind(instance):  # C44
    _bind(instance)
    folios, threads, slugs = h.specs_set()
    full = wire.build_batch(folios, threads, slugs)
    ms = h.manifest_over(folios, threads)
    site = next(f for f in full["folios"] if f["type"] == "site")
    finding = next(f for f in full["folios"] if f["type"] == "finding")
    ingest(instance, {"protocol": wire.PROTOCOL, "folios": [site], "threads": [],
                      "site_slugs": slugs, "manifest_signature": ms},
           verifier=_ok_verifier, require_signed=True)
    instance.store.revoke_binding(I, ALICE)
    b2 = {"protocol": wire.PROTOCOL, "folios": [finding], "threads": [], "site_slugs": {},
          "manifest_signature": ms}
    ack_revoked = ingest(instance, b2, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "revoked binding" for r in ack_revoked["rejected"])
    _bind(instance)
    ack_rebound = ingest(instance, copy.deepcopy(b2), verifier=_ok_verifier, require_signed=True)
    assert finding["content_hash"] in ack_rebound["accepted"]


# --- VM11/VM12: ingress totality over a hostile manifest --------------------


def test_non_dict_manifest_passes_shape_gate():  # VM11
    for bad in ("a-string", None, ["leaf"], 7):
        batch = {"protocol": wire.PROTOCOL, "folios": [], "threads": [],
                 "site_slugs": {}, "manifest_signature": bad}
        _validate_shape(batch)  # does not raise


@pytest.mark.parametrize("bad", [None, "a-string", ["sha256::" + "0" * 64]])
def test_non_dict_manifest_verdict_not_500(instance, bad):  # VM11
    _bind(instance)
    batch = _signed_batch()
    batch["manifest_signature"] = bad
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert ack["rejected"], "expected the folios to be rejected"
    assert all(r["reason"] == "manifest malformed" for r in ack["rejected"])
    assert not ack["accepted"] and not ack["threads"]["accepted"]
    assert instance.store.count_folios() == 0


def test_absent_manifest_key_is_no_manifest(instance):  # VM11
    _bind(instance)
    batch = _signed_batch()
    del batch["manifest_signature"]
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert ack["rejected"], "expected the folios to be rejected"
    assert all(r["reason"] == "no manifest" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_non_dict_manifest_off_unaffected(instance):  # VM11
    batch = _signed_batch()
    batch["manifest_signature"] = None
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=False)
    assert ack["accepted"], "OFF admits on integrity alone, manifest ignored"
    assert not ack["rejected"]


def test_malformed_manifest_verdict_not_500(instance):  # VM12
    _bind(instance)
    batch = _signed_batch()
    # tamper leaf_list so root no longer recomputes -> 'manifest malformed' per item
    batch["manifest_signature"]["leaf_list"] = ["sha256::" + "9" * 64]
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "manifest malformed" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


# --- FIX 1: OFF is manifest-blind — never invoke the verifier --------------


class SpyVerifier:
    def __init__(self, inner=None):
        self._inner = inner or h.ok_verifier
        self.calls = 0

    def __call__(self, canonical_bytes, bundle):
        self.calls += 1
        return self._inner(canonical_bytes, bundle)


def _raising_verifier(canonical_bytes, bundle):
    raise AssertionError("verify_wire_manifest must not be called under OFF")


def test_off_never_invokes_verifier(instance):  # FIX 1 (a)
    batch = _signed_batch()  # carries a valid manifest_signature
    spy = SpyVerifier()
    ack = ingest(instance, batch, verifier=spy, require_signed=False)
    assert spy.calls == 0
    assert len(ack["accepted"]) == 2 and ack["rejected"] == []
    assert len(ack["threads"]["accepted"]) == 1 and ack["threads"]["rejected"] == []


def test_off_raising_verifier_publishes(instance):  # FIX 1 (b)
    batch = _signed_batch()
    ack = ingest(instance, batch, verifier=_raising_verifier, require_signed=False)
    assert len(ack["accepted"]) == 2 and ack["rejected"] == []
    assert len(ack["threads"]["accepted"]) == 1


def test_on_invokes_verifier_exactly_once(instance):  # FIX 1 (c)
    _bind(instance)
    batch = _signed_batch()
    spy = SpyVerifier()
    ack = ingest(instance, batch, verifier=spy, require_signed=True)
    assert spy.calls == 1
    assert len(ack["accepted"]) == 2 and len(ack["threads"]["accepted"]) == 1


# --- FIX 2: global manifest failure rejects threads identically -------------


def test_on_malformed_manifest_rejects_all(instance):  # FIX 2 (a)
    """Every constituent -- folios and threads -- rejects with the identical
    'manifest malformed' reason."""
    _bind(instance)
    batch = _signed_batch()  # site folio + finding folio + the within thread
    batch["manifest_signature"] = 7  # non-dict -> WIRE-INTEGRITY 'manifest malformed'
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    all_rejected = ack["rejected"] + ack["threads"]["rejected"]
    assert len(ack["rejected"]) == 2 and len(ack["threads"]["rejected"]) == 1
    assert all(r["reason"] == "manifest malformed" for r in all_rejected)
    assert not ack["accepted"] and not ack["threads"]["accepted"]
    assert instance.store.count_folios() == 0


def test_on_thread_only_manifest_dangling_ok(instance):  # FIX 2 (b)
    """A valid manifest covering only the thread accepts it as dangling; the
    uncovered folios reject 'not in manifest'."""
    _bind(instance)
    folios, threads, slugs = h.specs_set()
    batch = wire.build_batch(folios, threads, slugs)
    assert batch["threads"], "need a within thread to exercise the dangling path"
    thread_hash = batch["threads"][0]["thread_hash"]
    batch["manifest_signature"] = sign_mod.sign_manifest([thread_hash], h.make_signer())
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert ack["rejected"] and all(r["reason"] == "not in manifest" for r in ack["rejected"])
    assert thread_hash in ack["threads"]["dangling"]
    assert thread_hash in ack["threads"]["accepted"]
    assert not ack["threads"]["rejected"]
    assert instance.store.count_folios() == 0


def test_on_unbound_signer_rejects_thread(instance):  # FIX 2 (c)
    batch = _signed_batch()  # site folio + finding folio + within thread
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    all_rejected = ack["rejected"] + ack["threads"]["rejected"]
    assert len(ack["rejected"]) == 2 and len(ack["threads"]["rejected"]) == 1
    assert all(r["reason"] == "unbound signer" for r in all_rejected)
    assert instance.store.count_folios() == 0
