"""Phase 4 test contract — the author-declared publish core (docs/PHASE_4_DESIGN.md §9).

RSP phase-2 spec: these tests ARE the contract for skein/publish.py. Failure-injection
tests assert a check FIRES; the Merkle test cross-checks against an independent
from-scratch RFC-6962 shadow (no shared imports with skein.canon).

Covered here: the pure/portable core (proposer, linter, physics floor, manifest +
signing with an injected fake signer). The API route, the thin CLI wrapper, and the
ingress accept-and-flag relaxation (§9 E/F) land with those pieces.
"""

import hashlib

import pytest

from skein import canon, publish
from skein.identity import compute_folio_hash, compute_thread_hash

CREATED_AT = "2026-07-04T00:00:00+00:00"


def _folio(title, content="body", ftype="finding"):
    f = {"type": ftype, "title": title, "content": content,
         "created_at": CREATED_AT, "created_by": "agent-x"}
    f["content_hash"] = compute_folio_hash(f)
    return f


def _thread(frm, to, ttype="reference", weaver=None, content=None):
    t = {"from_id": frm, "to_id": to, "type": ttype, "weaver": weaver,
         "created_at": CREATED_AT, "content": content}
    t["thread_hash"] = compute_thread_hash(frm, to, ttype, weaver, CREATED_AT, content)
    return t


# ── A. Author-declared assembly ─────────────────────────────────────────────
def test_A1_leaf_set_is_exactly_declared():
    a, b = _folio("A"), _folio("B")
    ref = _thread(a["content_hash"], b["content_hash"])
    leaves = set(publish.manifest_leaf_addresses([a, b], [ref]))
    expected = {publish.to_leaf_address(a["content_hash"]),
                publish.to_leaf_address(b["content_hash"]),
                publish.to_leaf_address(ref["thread_hash"])}
    assert leaves == expected
    # an undeclared edge contributes nothing
    stray = _thread(a["content_hash"], _folio("Z")["content_hash"])
    assert publish.to_leaf_address(stray["thread_hash"]) not in leaves


def test_A3_proposer_is_reachability_excluding_dangling_and_published():
    a, b, c = _folio("A"), _folio("B"), _folio("C")
    hA, hB, hC = a["content_hash"], b["content_hash"], c["content_hash"]
    ref = _thread(hA, hB)                                   # both selected -> in
    dangling = _thread(hA, hC)                              # C not selected -> out
    status = _thread(hA, hA, "status", content="closed")   # self-loop, both in -> in
    published = _thread(hA, "http://inst", "published")     # bookkeeping -> out
    got = {t["thread_hash"] for t in publish.propose_reachable(
        {hA, hB}, [ref, dangling, status, published])}
    assert got == {ref["thread_hash"], status["thread_hash"]}


# ── B. Linter — warns, never blocks ─────────────────────────────────────────
def test_B1_dangling_edge_warns():
    a, b, c = _folio("A"), _folio("B"), _folio("C")
    dangling = _thread(a["content_hash"], c["content_hash"])
    warns = publish.lint_declared_set([a, b], [dangling])
    assert any(w["code"] == "dangling" for w in warns)


def test_B2_slug_endpoint_warns():
    a = _folio("A")
    slug_edge = _thread(a["content_hash"], "some-local-slug")
    warns = publish.lint_declared_set([a], [slug_edge])
    assert any(w["code"] == "slug-endpoint" for w in warns)


def test_B3_status_selfloop_warns_looks_local():
    a = _folio("A")
    status = _thread(a["content_hash"], a["content_hash"], "status", content="closed")
    warns = publish.lint_declared_set([a], [status])
    assert any(w["code"] == "looks-local" for w in warns)


def test_B4_clean_structural_set_has_no_warnings():
    a, b = _folio("A"), _folio("B")
    ref = _thread(a["content_hash"], b["content_hash"])
    assert publish.lint_declared_set([a, b], [ref]) == []


# ── C. Physics floor — the only veto ────────────────────────────────────────
def test_C1_valid_set_passes_physics():
    a, b = _folio("A"), _folio("B")
    ref = _thread(a["content_hash"], b["content_hash"])
    publish.physics_check([a, b], [ref])  # no raise


def test_C1_tampered_folio_hash_fails_closed():
    a = _folio("A")
    a["content_hash"] = "sha256::" + "0" * 64
    with pytest.raises(publish.PhysicsError):
        publish.physics_check([a], [])


def test_C1_tampered_thread_hash_fails_closed():
    a, b = _folio("A"), _folio("B")
    ref = _thread(a["content_hash"], b["content_hash"])
    ref["thread_hash"] = "sha256::" + "1" * 64
    with pytest.raises(publish.PhysicsError):
        publish.physics_check([a, b], [ref])


# ── C3. Independent RFC-6962 Merkle shadow (no shared imports) ───────────────
def _shadow_root(addresses):
    """A from-scratch RFC-6962 §2.1 MTH over sha256::<hex> addresses. Shares no code
    with skein.canon: decode -> sort+dedup raw data -> tree with unwrapped odd split."""
    data = sorted({bytes.fromhex(a.split("::", 1)[1]) for a in addresses})

    def mth(leaves):
        if len(leaves) == 1:
            return hashlib.sha256(b"\x00" + leaves[0]).digest()
        k = 1
        while k * 2 < len(leaves):
            k *= 2
        return hashlib.sha256(b"\x01" + mth(leaves[:k]) + mth(leaves[k:])).digest()

    return "sha256::" + mth(data).hex()


def test_C3_merkle_root_matches_independent_shadow():
    a, b, c = _folio("A"), _folio("B"), _folio("C")
    ref = _thread(a["content_hash"], b["content_hash"])
    addrs = publish.manifest_leaf_addresses([a, b, c], [ref])
    assert canon.merkle_root_for_addresses(
        [publish.to_leaf_address(x) for x in addrs]) == _shadow_root(
        [publish.to_leaf_address(x) for x in addrs])


# ── D. Signing (injected fake signer — no real Sigstore) ────────────────────
class _FakeBundle:
    def model_dump_json(self):
        return '{"fake_bundle": true}'


class _FakeSigner:
    def __init__(self):
        self.calls = 0
        self.last_bytes = None

    def __call__(self, canonical_bytes):
        self.calls += 1
        self.last_bytes = canonical_bytes
        return publish.SignedResult(bundle=_FakeBundle(), issuer="iss@x", subject="sub")


def test_D1_sign_manifest_shape_and_single_ceremony():
    a, b = _folio("A"), _folio("B")
    ref = _thread(a["content_hash"], b["content_hash"])
    addrs = [publish.to_leaf_address(x)
             for x in publish.manifest_leaf_addresses([a, b], [ref])]
    signer = _FakeSigner()
    ms = publish.sign_manifest(addrs, signer)
    assert signer.calls == 1  # ONE descriptor, one ceremony
    assert set(ms["descriptor"]) == {"root", "leaf_count"}
    assert ms["descriptor"]["root"] == publish.build_manifest(addrs)["root"]
    assert ms["descriptor"]["leaf_count"] == 3
    assert ms["issuer"] == "iss@x" and ms["subject"] == "sub"
    # the signed bytes ARE the descriptor's canonical bytes (NOT the leaf list) —
    # assert on what the signer actually received, not a tautology
    assert signer.last_bytes == canon.manifest_descriptor_canonical_bytes(
        ms["descriptor"]["root"], ms["descriptor"]["leaf_count"])


def test_D4_over_max_leaves_fails_before_ceremony():
    signer = _FakeSigner()
    too_many = ["sha256::" + f"{i:064x}" for i in range(publish.MAX_LEAVES + 1)]
    with pytest.raises(ValueError):
        publish.sign_manifest(too_many, signer)
    assert signer.calls == 0  # never reached the signer


# ── C2. Manifest membership — independent shadow + tamper (fell-added) ───────
def _shadow_member(leaf_list, signed_root, constituent):
    """Independent membership: shadow-recompute the root AND check the constituent's
    raw datum is in the decoded leaf set. Shares no code with canon.manifest_membership."""
    data = {bytes.fromhex(a.split("::", 1)[1]) for a in leaf_list}
    return (_shadow_root(leaf_list) == signed_root
            and bytes.fromhex(constituent.split("::", 1)[1]) in data)


def test_C2_membership_matches_shadow_and_tamper_fails():
    a, b, c = _folio("A"), _folio("B"), _folio("C")
    addrs = [publish.to_leaf_address(x)
             for x in publish.manifest_leaf_addresses([a, b, c], [])]
    root = canon.merkle_root_for_addresses(addrs)
    member, nonmember = addrs[0], publish.to_leaf_address(_folio("Z")["content_hash"])
    assert canon.manifest_membership(addrs, root, member) is True
    assert _shadow_member(addrs, root, member) is True
    assert canon.manifest_membership(addrs, root, nonmember) is False
    assert _shadow_member(addrs, root, nonmember) is False
    # a tampered leaf_list no longer recomputes to the signed root -> not a member
    tampered = addrs[:-1] + [nonmember]
    assert canon.manifest_membership(tampered, root, member) is False


# ── C3 (extended). Odd-node split + empty-rejects the 4-leaf case can't reach ──
@pytest.mark.parametrize("n", [1, 2, 3, 5, 6, 7, 8])
def test_C3_merkle_matches_shadow_across_leaf_counts(n):
    addrs = ["sha256::" + hashlib.sha256(str(i).encode()).hexdigest() for i in range(n)]
    assert canon.merkle_root_for_addresses(addrs) == _shadow_root(addrs)


def test_C3_empty_manifest_is_rejected():
    with pytest.raises(canon.MerkleError):
        canon.merkle_root_for_addresses([])


# ── C1 (extended). physics_check is TOTAL — every malformed row is typed ─────
def test_C1_missing_content_hash_is_typed():
    a = _folio("A"); del a["content_hash"]
    with pytest.raises(publish.PhysicsError):
        publish.physics_check([a], [])


def test_C1_none_content_hash_is_typed():
    a = _folio("A"); a["content_hash"] = None
    with pytest.raises(publish.PhysicsError):
        publish.physics_check([a], [])


def test_C1_nonstr_field_is_typed():
    a = _folio("A"); a["type"] = 123  # canon rejects non-str -> PhysicsError, not 500
    with pytest.raises(publish.PhysicsError):
        publish.physics_check([a], [])


# ── E. The orchestrator — signature un-forgettable, physics before signer ────
def test_E_orchestrator_attaches_signature_over_exactly_the_declared_set():
    a, b = _folio("A"), _folio("B")
    ref = _thread(a["content_hash"], b["content_hash"])
    signer = _FakeSigner()
    batch = publish.assemble_signed_batch([a, b], [ref], {}, signer)
    assert "manifest_signature" in batch
    assert signer.calls == 1
    declared = set(publish.manifest_leaf_addresses([a, b], [ref]))
    assert set(batch["manifest_signature"]["leaf_list"]) == declared


def test_E_orchestrator_physics_first_never_reaches_signer():
    a = _folio("A"); a["content_hash"] = "sha256::" + "0" * 64
    signer = _FakeSigner()
    with pytest.raises(publish.PhysicsError):
        publish.assemble_signed_batch([a], [], {}, signer)
    assert signer.calls == 0  # no Rekor entry burned on a bad batch


def test_domain_separation_binds_the_manifest_profile():
    assert publish.CANON_PROFILE_MANIFEST_V1 == "skein.manifest.canon/v1"
    assert publish.profiled_preimage("skein.manifest.canon/v1", b"body") == \
        b"skein.manifest.canon/v1\x00body"


# ── round-2 fell fixes ──────────────────────────────────────────────────────
def test_C1_nonmapping_row_is_typed():
    # a non-dict declared row (e.g. a not-found resolve returned as None) must be a
    # typed PhysicsError, not a raw AttributeError 500
    with pytest.raises(publish.PhysicsError):
        publish.physics_check([None], [])


def test_B_linter_is_total_over_malformed_folio():
    # the advisory linter must NEVER raise, even on a hashless / non-mapping row
    assert isinstance(publish.lint_declared_set([{"type": "finding"}], []), list)
    assert isinstance(publish.lint_declared_set([None], [None]), list)


def test_B_dangling_selfloop_to_undeclared_folio_warns():
    # a content-hash self-loop whose folio isn't in the publish IS dangling
    a, z = _folio("A"), _folio("Z")
    selfloop = _thread(z["content_hash"], z["content_hash"], "reference")
    warns = publish.lint_declared_set([a], [selfloop])
    assert any(w["code"] == "dangling" for w in warns)


def test_wire_serializes_datetime_created_at_to_a_string():
    # the store returns a datetime created_at; the wire must be JSON-serializable AND
    # re-hash identically (regression for the fell's datetime->500 finding)
    import json as _json
    from datetime import datetime, timezone
    dt = datetime(2026, 7, 4, tzinfo=timezone.utc)
    folio = {"content_hash": "sha256::" + "a" * 64, "type": "finding", "title": "T",
             "content": "b", "created_at": dt, "created_by": "x"}
    thread = {"thread_hash": "sha256::" + "b" * 64, "from_id": "sha256::" + "a" * 64,
              "to_id": "sha256::" + "a" * 64, "type": "reference", "weaver": None,
              "created_at": dt, "content": None}
    batch = publish.build_batch([folio], [thread], {})
    assert isinstance(batch["folios"][0]["created_at"], str)
    assert isinstance(batch["threads"][0]["created_at"], str)
    _json.dumps(batch)  # must not raise TypeError


def test_wire_datetime_normalization_still_rehashes_on_the_receiver():
    # the strong check: after normalizing a datetime created_at to the wire string, the
    # RECEIVER's integrity floor (skein_next.wire) must still recompute the same hash —
    # i.e. the normalization is idempotent across the two trees, not just serializable.
    from datetime import datetime, timezone
    from skein_next import wire as nx_wire
    dt = datetime(2026, 7, 4, 5, 6, 7, 123456, tzinfo=timezone.utc)
    fields = {"type": "finding", "title": "T", "content": "b",
              "created_at": dt, "created_by": "x"}
    folio = dict(fields, content_hash=compute_folio_hash(fields))
    wire_folio = publish.build_batch([folio], [], {})["folios"][0]
    assert nx_wire.folio_reject_reason(wire_folio) is None  # receiver accepts the re-hash
