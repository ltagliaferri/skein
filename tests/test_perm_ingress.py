"""Permission model — by-ends authorization wired into the ingress (rev 6 §8).

End-to-end over ingest() under require_signed: the fake ok_verifier fixes the signer to
ALICE, so a FOREIGN lineage/slug is modelled by pre-seeding it owned by BOB (a direct
attribution). Proves the wiring — signer plumbed to authorize_thread, staged graph,
live tier/grant reads, reject reasons surfaced in the ack — not just the pure predicate.
"""

from __future__ import annotations

import hashlib

import pytest

from skein import wire
from skein.identity import compute_thread_hash
from skein.ingress import ingest
from skein.station import Station
from tests import station_publish_helpers as h
from tests.station_publish_helpers import ALICE, I

BOB = "bob@example.com"
ADMIN = "admin@example.com"


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein-next")
    yield s
    s.close()


def _bind(instance, subject=ALICE, role="originator"):
    instance.store.add_binding(
        I, subject, role=role, vouched_by_issuer=I, vouched_by_subject=I
    )


def _seed_owned(instance, owner_subject, title, ftype="finding"):
    """Create a stored folio attributed to owner_subject — a foreign lineage genesis."""
    f = h.folio(ftype, title, "body", "2026-01-01T00:00:00+00:00")
    ch = f["content_hash"]
    instance.store.create_folio(f)
    root = "sha256::" + hashlib.sha256(owner_subject.encode()).hexdigest()
    mh = "sha256::" + hashlib.sha256((root + ch).encode()).hexdigest()
    instance.store.add_manifest(root, mh, "{}", "[]", "{}", I, owner_subject, 1)
    instance.store.add_constituent_attribution(ch, "folio", root, I, owner_subject)
    return ch


def _supersedes(new, old, created_at="2026-02-01T00:00:00+00:00"):
    th = compute_thread_hash(
        from_id=new, to_id=old, type="supersedes",
        weaver=None, created_at=created_at, content=None,
    )
    return {
        "thread_hash": th, "from_id": new, "to_id": old, "type": "supersedes",
        "weaver": None, "created_at": created_at, "content": None,
    }


def _alice_supersede_batch(old_hash):
    """ALICE publishes a fresh version + a supersedes onto ``old_hash``."""
    newv = h.folio("finding", "alice v2", "newbody", "2026-02-01T00:00:00+00:00")
    st = _supersedes(newv["content_hash"], old_hash)
    batch = wire.build_batch([newv], [st])
    batch["manifest_signature"] = h.manifest_over([newv], [st])
    return batch, newv["content_hash"], st["thread_hash"]


def test_ingress_rejects_supersede_on_foreign_lineage(instance):
    _bind(instance)  # ALICE is a bound originator
    victim = _seed_owned(instance, BOB, "bob genesis")
    batch, newv, st = _alice_supersede_batch(victim)
    ack = ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    assert newv in ack["accepted"]                     # ALICE may create her own folio
    assert not ack["threads"]["accepted"]              # but not supersede bob's lineage
    assert ack["threads"]["rejected"][0]["thread_hash"] == st
    assert "no edit access" in ack["threads"]["rejected"][0]["reason"]


def test_ingress_grant_allows_supersede(instance):
    _bind(instance)
    victim = _seed_owned(instance, BOB, "bob genesis")
    instance.store.add_grant(victim, I, ALICE, "supersede", I, ADMIN)  # ALICE granted
    batch, newv, st = _alice_supersede_batch(victim)
    ack = ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    assert st in ack["threads"]["accepted"]


def test_ingress_revoked_grant_blocks_supersede(instance):
    _bind(instance)
    victim = _seed_owned(instance, BOB, "bob genesis")
    instance.store.add_grant(victim, I, ALICE, "supersede", I, ADMIN)
    instance.store.revoke_grant(victim, I, ALICE, "supersede")
    batch, newv, st = _alice_supersede_batch(victim)
    ack = ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    assert st in [r["thread_hash"] for r in ack["threads"]["rejected"]]


def test_ingress_steward_may_supersede_foreign_lineage(instance):
    _bind(instance, role="steward")  # ALICE is a steward
    victim = _seed_owned(instance, BOB, "bob genesis")
    batch, newv, st = _alice_supersede_batch(victim)
    ack = ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    assert st in ack["threads"]["accepted"]


def test_ingress_slug_collision_preserves_existing_claim(instance):
    _bind(instance)
    bob_site = _seed_owned(instance, BOB, "bob specs site", ftype="site")
    instance.store.claim_slug("specs", bob_site, I, BOB)  # BOB owns the slug
    # ALICE publishes a DIFFERENT site folio under the same slug "specs".
    folios, threads, slugs = h.specs_set(site_title="alice's specs")
    batch = wire.build_batch(folios, threads, slugs)
    batch["manifest_signature"] = h.manifest_over(folios, threads)
    ack = ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    # ALICE's site folio is still stored (content is valid)...
    alice_site = next(f["content_hash"] for f in folios if f["type"] == "site")
    assert alice_site in ack["accepted"]
    # ...but the slug still belongs to BOB (collision — last-write does NOT win).
    claim = instance.store.get_slug_claim("specs")
    assert claim["claimed_by_subject"] == BOB and claim["anchor_hash"] == bob_site


def test_ingress_slug_stays_genesis_anchored_across_supersession(instance):
    """fell-r4: publishing a superseding site version under the same slug keeps the slug
    ANCHORED AT THE GENESIS (resolve_slug derives the head), so members filed under the
    genesis keep their breadcrumb (§5.2) — not re-anchored to the published head."""
    _bind(instance)
    site_v1 = h.folio("site", "site v1", "b", "2026-01-01T00:00:00+00:00")
    member = h.folio("finding", "member", "b", "2026-01-02T00:00:00+00:00")
    win = h.within(member["content_hash"], site_v1["content_hash"], "2026-01-03T00:00:00+00:00")
    b1 = wire.build_batch([site_v1, member], [win], {site_v1["content_hash"]: "specs"})
    b1["manifest_signature"] = h.manifest_over([site_v1, member], [win])
    ingest(instance, b1, verifier=h.ok_verifier, require_signed=True)
    assert instance.store.resolve_slug("specs") == site_v1["content_hash"]
    assert instance.store.folio_site_slug(member["content_hash"]) == "specs"

    # publish v2 superseding v1, under the SAME slug
    site_v2 = h.folio("site", "site v2", "b", "2026-02-01T00:00:00+00:00")
    sup = _supersedes(site_v2["content_hash"], site_v1["content_hash"])
    b2 = wire.build_batch([site_v2], [sup], {site_v2["content_hash"]: "specs"})
    b2["manifest_signature"] = h.manifest_over([site_v2], [sup])
    ack = ingest(instance, b2, verifier=h.ok_verifier, require_signed=True)
    assert sup["thread_hash"] in ack["threads"]["accepted"]
    # the ANCHOR stays the genesis (v1); the derived head is v2
    assert instance.store.get_slug_claim("specs")["anchor_hash"] == site_v1["content_hash"]
    assert instance.store.resolve_slug("specs") == site_v2["content_hash"]
    # the member filed under v1 KEEPS its breadcrumb
    assert instance.store.folio_site_slug(member["content_hash"]) == "specs"


def test_ingress_same_batch_supersede_and_within_authorize(instance):
    """A same-batch supersede + within by the site owner both authorize (the staged
    graph does not wrong-block a legit same-batch filing)."""
    _bind(instance)
    site_g = _seed_owned(instance, ALICE, "alice site v1", ftype="site")
    site_v2 = h.folio("site", "alice site v2", "body", "2026-02-01T00:00:00+00:00")
    member = h.folio("finding", "member", "body", "2026-02-02T00:00:00+00:00")
    sup = _supersedes(site_v2["content_hash"], site_g)
    win = h.within(member["content_hash"], site_g, "2026-02-03T00:00:00+00:00")
    batch = wire.build_batch([site_v2, member], [sup, win])
    batch["manifest_signature"] = h.manifest_over([site_v2, member], [sup, win])
    ack = ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    assert sup["thread_hash"] in ack["threads"]["accepted"]
    assert win["thread_hash"] in ack["threads"]["accepted"]
