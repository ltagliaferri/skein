"""Thread E — the unified require_signed decision table (RS1-RS20), the accepted
replay stances (C40/C41/C42/C44), and the ingress totality (VM11/VM12).

ONE table over CONSTITUENTS (folios AND threads judged identically by membership).
OFF is manifest-blind AND binding-blind (byte-identical to pre-mesh); ON admits a
constituent iff it is a leaf under a manifest that verifies AND whose signer is
bound + non-revoked, gating the binding ONCE per manifest.
"""

from __future__ import annotations

import copy

import pytest

from skein import signing
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus

from skein_next import canon, profile, wire
from skein_next import sign as sign_mod
from skein_next.station import Station
from skein_next.ingress import ingest, _validate_shape, BatchShapeError, create_app


I = "https://accounts.google.com"
ALICE = "alice@example.com"


# --- signer / verifier fakes ------------------------------------------------


def _signer(issuer=I, subject=ALICE):
    def s(canonical_bytes):
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, canonical_bytes)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)
    return s


def _ok_verifier(canonical_bytes, bundle):
    return MultiVerifyResult(
        results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
        overall=VerifyStatus.VERIFIED,
    )


def _bad_verifier(canonical_bytes, bundle):
    return MultiVerifyResult(
        results=[VerifyResult(status=VerifyStatus.SIGNATURE_MISMATCH)],
        overall=VerifyStatus.SIGNATURE_MISMATCH,
    )


class SpyBindings:
    def __init__(self, store):
        self._store = store
        self.get_binding_calls = 0

    def get_binding(self, issuer, subject):
        self.get_binding_calls += 1
        return self._store.get_binding(issuer, subject)


@pytest.fixture
def client(tmp_path):
    s = Station(tmp_path / "client" / ".skein-next")
    yield s
    s.close()


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein-next")
    yield s
    s.close()


def _seed(station):
    station.create_site("specs", purpose="Public specs", created_by="t")
    f = station.post("finding", "specs", "Design Overview", "body", created_by="t")
    return f


def _signed_batch(client, signer=None, addresses_override=None):
    """A publish batch with a manifest_signature over its constituents."""
    from skein_next import publish as pub

    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    signer = signer or _signer()
    addrs = addresses_override or (
        [f["content_hash"] for f in batch["folios"]]
        + [t["thread_hash"] for t in batch["threads"]]
    )
    batch["manifest_signature"] = sign_mod.sign_manifest(addrs, signer)
    return batch


def _bind(instance, issuer=I, subject=ALICE, role="author"):
    instance.store.add_binding(issuer, subject, role=role,
                               vouched_by_issuer=issuer, vouched_by_subject=subject)


# --- RS1-RS12: the unified decision table -----------------------------------


def test_off_unsigned_constituent_accepts(client, instance):  # RS1
    _seed(client)
    batch = wire.build_batch(*__import__("skein_next.publish", fromlist=["x"]).collect_publish_set(client, site="specs"))
    ack = ingest(instance, batch, require_signed=False)
    assert len(ack["accepted"]) == 2 and ack["rejected"] == []


def test_on_no_manifest_rejects(client, instance):  # RS2
    _seed(client)
    from skein_next import publish as pub
    batch = wire.build_batch(*pub.collect_publish_set(client, site="specs"))
    ack = ingest(instance, batch, require_signed=True)
    assert ack["accepted"] == []
    assert all(r["reason"] == "no manifest" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_manifest_bad_signature_rejects(client, instance):  # RS3
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_bad_verifier, require_signed=True)
    assert ack["accepted"] == []
    assert all(r["reason"] == "manifest signature SIGNATURE_MISMATCH" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_manifest_signer_unbound_rejects(client, instance):  # RS4
    _seed(client)  # no binding added
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "unbound signer" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_manifest_signer_revoked_rejects(client, instance):  # RS5
    _seed(client)
    _bind(instance)
    instance.store.revoke_binding(I, ALICE)
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "revoked binding" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_leaf_not_in_manifest_rejects(client, instance):  # RS6
    _seed(client)
    _bind(instance)
    from skein_next import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    # manifest covers ONLY the site folio, not the finding
    site_hash = next(f["content_hash"] for f in batch["folios"]
                     if f["type"] == "site")
    batch["manifest_signature"] = sign_mod.sign_manifest([site_hash], _signer())
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    reasons = {r["content_hash"]: r["reason"] for r in ack["rejected"]}
    finding = next(f["content_hash"] for f in batch["folios"] if f["type"] == "finding")
    assert reasons[finding] == "not in manifest"
    # threads referencing the finding are also rejected (not in manifest)


def test_on_covered_bound_constituent_accepts(client, instance):  # RS7
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert len(ack["accepted"]) == 2
    for h in ack["accepted"]:
        proof = instance.store.get_constituent_proof(h)
        assert proof["subject"] == ALICE and proof["proof_missing"] is False


def test_on_manifest_binding_gated_once(client, instance):  # RS8
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    spy = SpyBindings(instance.store)
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True, bindings=spy)
    assert spy.get_binding_calls == 1


def test_off_never_consults_bindings_or_manifest(client, instance):  # RS9
    _seed(client)
    batch = _signed_batch(client)  # carries a manifest, but OFF must ignore it
    spy = SpyBindings(instance.store)
    ack = ingest(instance, batch, verifier=_bad_verifier, require_signed=False, bindings=spy)
    assert spy.get_binding_calls == 0
    assert len(ack["accepted"]) == 2 and ack["rejected"] == []


def test_reject_reasons_pairwise_distinct():  # RS10
    from skein_next.ingress import _constituent_manifest_reject_reason as R
    authz = {"no manifest", "manifest signature SIGNATURE_MISMATCH",
             "unbound signer", "revoked binding", "not in manifest"}
    wire_integrity = {"hash mismatch", "invalid fields", "dangling endpoint"}
    manifest_wire = {"manifest malformed", "wrong kind", "unknown profile"}
    # the three manifest-failure buckets are DISJOINT
    assert R("no manifest") == "no manifest"
    assert R("manifest malformed") == "manifest malformed"  # bare, NOT wrapped
    assert R("wrong kind") == "wrong kind"
    assert R("unknown profile") == "unknown profile"
    assert R("SIGNATURE_MISMATCH") == "manifest signature SIGNATURE_MISMATCH"
    all_reasons = authz | wire_integrity | manifest_wire
    assert len(all_reasons) == 11  # all pairwise distinct


def test_attribution_is_manifest_signer_not_created_by_or_weaver(client, instance):  # RS11
    client.create_site("specs", purpose="p", created_by="t")
    f = client.post("finding", "specs", "T", "b", created_by="mallory")
    from skein_next import publish as pub
    _bind(instance)
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    addrs = [x["content_hash"] for x in batch["folios"]] + [t["thread_hash"] for t in batch["threads"]]
    batch["manifest_signature"] = sign_mod.sign_manifest(addrs, _signer())
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    proof = instance.store.get_constituent_proof(f)
    assert proof["issuer"] == I and proof["subject"] == ALICE  # NOT 'mallory'


def test_ingest_default_bindings_is_station_store(client, instance):  # RS12
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)  # no bindings=
    assert len(ack["accepted"]) == 2


# --- RS13-RS20: isolation, idempotency, OFF->ON attribution, slug gate -------


def test_per_item_savepoint_isolation(client, instance):  # RS13
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    # malform ONE folio body so it rejects, sibling still commits; HTTP-equivalent 200
    batch["folios"][0]["content_hash"] = "sha256::" + "0" * 64  # hash mismatch
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert len(ack["rejected"]) >= 1
    # the sibling folio still landed
    assert len(ack["accepted"]) >= 1


def test_old_manifest_replay_cannot_inject_new_leaf(client, instance):  # RS14
    _seed(client)
    _bind(instance)
    # manifest covers only the site folio + finding (publish 1)
    batch1 = _signed_batch(client)
    ingest(instance, batch1, verifier=_ok_verifier, require_signed=True)
    # a NEW folio not in the old manifest, replayed under the OLD manifest_signature
    l2 = client.post("finding", "specs", "New", "newbody", created_by="t")
    from skein_next import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch2 = wire.build_batch(folios, threads, slugs)
    batch2["manifest_signature"] = batch1["manifest_signature"]  # OLD manifest
    ack = ingest(instance, batch2, verifier=_ok_verifier, require_signed=True)
    rejected = {r["content_hash"]: r["reason"] for r in ack["rejected"]}
    assert rejected.get(l2) == "not in manifest"


def test_same_manifest_idempotent_republish(client, instance):  # RS15
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    again = ingest(instance, copy.deepcopy(batch), verifier=_ok_verifier, require_signed=True)
    assert again["accepted"] == []
    assert len(again["existing"]) == 2
    # one manifest row, attribution unchanged
    root = batch["manifest_signature"]["descriptor"]["root"]
    assert len(instance.store.get_manifest_proofs_by_root(root)) == 1


def test_mixed_and_single_kind_batches_gated(client, instance):  # RS16
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)  # site folio + finding + the 'within' thread
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    # folios AND the thread are all members -> all accept
    assert len(ack["accepted"]) == 2
    assert len(ack["threads"]["accepted"]) == 1  # the within edge is a leaf too
    # a thread-only batch with NO manifest under ON -> 'no manifest'
    from skein_next import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    thread_only = {"protocol": wire.PROTOCOL, "folios": [], "threads": threads,
                   "site_slugs": {}}
    ack2 = ingest(instance, thread_only, require_signed=True)
    assert all(r["reason"] == "no manifest" for r in ack2["threads"]["rejected"])


def test_slug_cross_author_overwrite_is_last_write_wins_v0(client, instance):  # RS19
    _seed(client)
    _bind(instance)
    ingest(instance, _signed_batch(client), verifier=_ok_verifier, require_signed=True)
    first_site = instance.store.resolve_slug("specs")
    # a second bound author publishes a DIFFERENT site folio under the same slug
    client2 = Station(client.store.data_dir.parent.parent / "client2" / ".skein-next")
    s2 = client2.create_site("specs", purpose="DIFFERENT", created_by="bob")
    from skein_next import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client2, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in batch["folios"]] + [t["thread_hash"] for t in batch["threads"]]
    batch["manifest_signature"] = sign_mod.sign_manifest(addrs, _signer())
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert instance.store.resolve_slug("specs") == s2  # last write wins
    assert s2 != first_site
    client2.close()


def test_ingress_exposes_no_binding_mutation_route():  # RS17
    app = create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any("account" in p or "binding" in p for p in paths)


def test_off_then_on_existing_body_acquires_attribution(client, instance):  # RS20
    _seed(client)
    f = client.store.list_folios()[0]["content_hash"] if False else None
    from skein_next import publish as pub
    # 1) ingest the finding under OFF (no manifest, no attribution)
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    off_batch = wire.build_batch(folios, threads, slugs)
    ingest(instance, off_batch, require_signed=False)
    finding = next(x["content_hash"] for x in off_batch["folios"] if x["type"] == "finding")
    assert instance.store.get_constituent_proof(finding) is None  # UNSIGNED
    # 2) re-deliver under ON inside a valid bound covering manifest
    _bind(instance)
    on_batch = _signed_batch(client)
    ack = ingest(instance, on_batch, verifier=_ok_verifier, require_signed=True)
    assert finding in ack["existing"]  # body de-dups
    proof = instance.store.get_constituent_proof(finding)  # but now attributed
    assert proof is not None and proof["subject"] == ALICE


def test_off_then_on_unverified_manifest_stays_unattributed(client, instance):  # RS20 negative
    _seed(client)
    from skein_next import publish as pub
    off_batch = wire.build_batch(*pub.collect_publish_set(client, site="specs"))
    ingest(instance, off_batch, require_signed=False)
    finding = next(x["content_hash"] for x in off_batch["folios"] if x["type"] == "finding")
    _bind(instance)
    on_batch = _signed_batch(client)
    ingest(instance, on_batch, verifier=_bad_verifier, require_signed=True)  # crypto fails
    assert instance.store.get_constituent_proof(finding) is None  # stays UNSIGNED


def test_on_slug_write_gated_by_site_folio_membership(client, instance):  # RS18
    _seed(client)
    _bind(instance)
    from skein_next import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    finding = next(f["content_hash"] for f in batch["folios"] if f["type"] == "finding")
    # manifest covers ONLY the finding, NOT the site folio -> site folio rejected
    batch["manifest_signature"] = sign_mod.sign_manifest([finding], _signer())
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert instance.store.resolve_slug("specs") is None  # no slug landed


# --- C40-C44: accepted replay stances ---------------------------------------


def test_subset_replay_of_own_manifest_accepted(client, instance):  # C41
    _seed(client)
    _bind(instance)
    from skein_next import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    full = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in full["folios"]] + [t["thread_hash"] for t in full["threads"]]
    ms = sign_mod.sign_manifest(addrs, _signer())
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


def test_revoked_between_publishes_re_gates_per_ingest(client, instance):  # C40
    _seed(client)
    _bind(instance)
    from skein_next import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    full = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in full["folios"]] + [t["thread_hash"] for t in full["threads"]]
    ms = sign_mod.sign_manifest(addrs, _signer())
    site = next(f for f in full["folios"] if f["type"] == "site")
    finding = next(f for f in full["folios"] if f["type"] == "finding")
    b1 = {"protocol": wire.PROTOCOL, "folios": [site], "threads": [], "site_slugs": slugs,
          "manifest_signature": ms}
    ingest(instance, b1, verifier=_ok_verifier, require_signed=True)
    # signer revoked AFTER publish 1
    instance.store.revoke_binding(I, ALICE)
    b2 = {"protocol": wire.PROTOCOL, "folios": [finding], "threads": [], "site_slugs": {},
          "manifest_signature": ms}
    ack = ingest(instance, b2, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "revoked binding" for r in ack["rejected"])  # re-gated live


def test_cross_instance_replay_contained_by_per_instance_binding(client, tmp_path):  # C42
    _seed(client)
    batch = _signed_batch(client)
    # instance A: signer bound -> accept
    a = Station(tmp_path / "A" / ".skein-next")
    a.store.add_binding(I, ALICE, role="author")
    ackA = ingest(a, copy.deepcopy(batch), verifier=_ok_verifier, require_signed=True)
    assert len(ackA["accepted"]) == 2
    a.close()
    # instance B: signer NOT bound -> reject 'unbound signer'
    b = Station(tmp_path / "B" / ".skein-next")
    ackB = ingest(b, copy.deepcopy(batch), verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "unbound signer" for r in ackB["rejected"])
    b.close()


def test_reactivation_replay_accepted_on_rebind(client, instance):  # C44
    _seed(client)
    _bind(instance)
    from skein_next import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    full = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in full["folios"]] + [t["thread_hash"] for t in full["threads"]]
    ms = sign_mod.sign_manifest(addrs, _signer())
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
    # re-add (reactivate) and replay again -> accepts
    _bind(instance)
    ack_rebound = ingest(instance, copy.deepcopy(b2), verifier=_ok_verifier, require_signed=True)
    assert finding["content_hash"] in ack_rebound["accepted"]


# --- VM11/VM12: ingress totality over a hostile manifest --------------------


def test_ingress_non_dict_manifest_signature_is_400_not_500(client):  # VM11
    batch = {"protocol": wire.PROTOCOL, "folios": [], "threads": [],
             "site_slugs": {}, "manifest_signature": "a-string"}
    with pytest.raises(BatchShapeError):
        _validate_shape(batch)


def test_ingress_malformed_manifest_per_verdict_not_500(client, instance):  # VM12
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    # tamper leaf_list so root no longer recomputes -> 'manifest malformed' per item
    batch["manifest_signature"]["leaf_list"] = ["sha256::" + "9" * 64]
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "manifest malformed" for r in ack["rejected"])
    assert instance.store.count_folios() == 0
