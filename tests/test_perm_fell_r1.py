"""Permission model — regression tests for the fell-r1 findings (opus + codex + glm).

1  legacy folio capture: a bound non-owner re-publishing a legacy (unattributed) folio
   must NOT gain its attribution and the power to supersede it.
2  migration atomicity: a mid-run failure rolls back fully (corpus untouched), and a
   re-run recovers (no orphan *_new tables).
3  OFF merge honest ack: a merge whose second parent the <=1-parent index drops must be
   reported REJECTED, never a false 'accepted'.
"""

from __future__ import annotations

import sqlite3

import pytest

from skein import wire
from skein.identity import compute_thread_hash
from skein.ingress import ingest
from skein.migrations import perm_model_rev6
from skein.station import Station
from tests import station_publish_helpers as h
from tests.station_publish_helpers import ALICE, I
from tests.test_perm_migration import _old_shape_db

BOB = "bob@example.com"


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein-next")
    yield s
    s.close()


def _bind(instance, subject=ALICE, role="originator"):
    instance.store.add_binding(I, subject, role=role, vouched_by_issuer=I, vouched_by_subject=I)


def _supersedes(new, old, created_at="2026-02-01T00:00:00+00:00"):
    th = compute_thread_hash(from_id=new, to_id=old, type="supersedes",
                             weaver=None, created_at=created_at, content=None)
    return {"thread_hash": th, "from_id": new, "to_id": old, "type": "supersedes",
            "weaver": None, "created_at": created_at, "content": None}


# --- finding 1: legacy folio capture ----------------------------------------


def test_legacy_folio_not_capturable_via_republish(instance):
    """A legacy (OFF-era, unattributed) site folio F. Alice (bound originator, no tier,
    no grant) re-publishes F's public bytes + a new folio N + supersedes(N -> F). She
    must NOT capture F: F stays owner-None, so her supersede onto it is rejected."""
    # 1) F exists from the OFF era (stored, unattributed).
    legacy_site = h.folio("site", "legacy site", "body", "2026-01-01T00:00:00+00:00")
    F = legacy_site["content_hash"]
    ingest(instance, wire.build_batch([legacy_site], []), require_signed=False)
    assert instance.store.get_constituent_proof(F) is None  # owner-None

    # 2) Alice re-publishes F's bytes + her new version N + supersedes(N -> F), signed.
    _bind(instance)
    N = h.folio("finding", "alice version", "nb", "2026-02-01T00:00:00+00:00")
    st = _supersedes(N["content_hash"], F)
    batch = wire.build_batch([legacy_site, N], [st])
    batch["manifest_signature"] = h.manifest_over([legacy_site, N], [st])
    ack = ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)

    # F did NOT gain Alice's attribution — it stays owner-None.
    assert instance.store.get_constituent_proof(F) is None
    # and the supersede onto the legacy lineage is rejected (no edit access).
    assert st["thread_hash"] in [r["thread_hash"] for r in ack["threads"]["rejected"]]
    assert not ack["threads"]["accepted"]


def test_legacy_folio_moderable_by_steward(instance):
    """The legacy-moderation path stays reachable: a steward CAN supersede a legacy
    owner-None lineage (tier path), confirming the fix did not over-block."""
    legacy_site = h.folio("site", "legacy site", "body", "2026-01-01T00:00:00+00:00")
    F = legacy_site["content_hash"]
    ingest(instance, wire.build_batch([legacy_site], []), require_signed=False)
    _bind(instance, role="steward")
    N = h.folio("finding", "steward version", "nb", "2026-02-01T00:00:00+00:00")
    st = _supersedes(N["content_hash"], F)
    batch = wire.build_batch([legacy_site, N], [st])
    batch["manifest_signature"] = h.manifest_over([legacy_site, N], [st])
    ack = ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    assert st["thread_hash"] in ack["threads"]["accepted"]


def test_legacy_site_not_nameable_by_non_owner(instance):
    """audit-cap: a signer who merely RE-PUBLISHES a legacy owner-None site must not be
    recorded as its slug claimant — else they could same-signer re-anchor that name onto
    a lineage of their own. Naming follows ownership; a legacy site is tier-nameable."""
    legacy_site = h.folio("site", "legacy site", "b", "2026-01-01T00:00:00+00:00")
    F = legacy_site["content_hash"]
    ingest(instance, wire.build_batch([legacy_site], []), require_signed=False)
    assert instance.store.get_constituent_proof(F) is None  # owner-None

    _bind(instance)  # ALICE: bound originator, no tier
    batch = wire.build_batch([legacy_site], [], {F: "specs"})
    batch["manifest_signature"] = h.manifest_over([legacy_site], [])
    ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    # no claim recorded — ALICE does not own the legacy site
    assert instance.store.get_slug_claim("specs") is None


def _legacy_site_slug_batch(instance):
    legacy_site = h.folio("site", "legacy site", "b", "2026-01-01T00:00:00+00:00")
    F = legacy_site["content_hash"]
    ingest(instance, wire.build_batch([legacy_site], []), require_signed=False)
    batch = wire.build_batch([legacy_site], [], {F: "specs"})
    batch["manifest_signature"] = h.manifest_over([legacy_site], [])
    return batch, F


def test_legacy_site_nameable_by_administrator(instance):
    """The OVERRIDE path stays reachable: an administrator CAN name a legacy owner-None
    site. Naming an unowned lineage is the §6 admin/operator override — NOT a steward
    power (see the steward case below), so this binds administrator deliberately."""
    batch, F = _legacy_site_slug_batch(instance)
    instance.store.add_binding(I, ALICE, role="administrator",
                               vouched_by_issuer=I, vouched_by_subject=I)
    ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    assert instance.store.get_slug_claim("specs")["anchor_hash"] == F


def test_legacy_site_not_nameable_by_steward(instance):
    """§6 scopes the slug override to administrator/operator ONLY — a steward may
    moderate a lineage but may NOT seize its name. Pins the boundary so the override set
    and _TIER_MAY_ACT are not silently conflated."""
    batch, _F = _legacy_site_slug_batch(instance)
    instance.store.add_binding(I, ALICE, role="steward",
                               vouched_by_issuer=I, vouched_by_subject=I)
    ingest(instance, batch, verifier=h.ok_verifier, require_signed=True)
    assert instance.store.get_slug_claim("specs") is None


# --- finding 3: OFF merge honest ack ----------------------------------------


def test_off_merge_reported_rejected_not_accepted(instance):
    """Under OFF, two supersedes sharing a from_id (a merge): the <=1-parent index drops
    the second. The ack must report it REJECTED, never a false 'accepted' with a row
    that was not persisted."""
    p1 = h.folio("finding", "p1", "b", "2026-01-01T00:00:00+00:00")
    p2 = h.folio("finding", "p2", "b", "2026-01-02T00:00:00+00:00")
    child = h.folio("finding", "child", "b", "2026-01-03T00:00:00+00:00")
    s1 = _supersedes(child["content_hash"], p1["content_hash"])
    s2 = _supersedes(child["content_hash"], p2["content_hash"])  # second parent = merge
    batch = wire.build_batch([p1, p2, child], [s1, s2])
    ack = ingest(instance, batch, require_signed=False)
    accepted = set(ack["threads"]["accepted"])
    rejected = {r["thread_hash"] for r in ack["threads"]["rejected"]}
    # exactly one parent persisted; the other reported rejected (not silently accepted)
    persisted = [t for t in (s1["thread_hash"], s2["thread_hash"]) if t in accepted]
    assert len(persisted) == 1
    assert len(rejected & {s1["thread_hash"], s2["thread_hash"]}) == 1
    # the ack must not claim a row it did not store
    for t in accepted:
        assert instance.store.get_thread(t) is not None


# --- finding 2: migration atomicity -----------------------------------------


def test_migration_rolls_back_fully_on_failure(tmp_path, monkeypatch):
    """A failure mid-migration leaves the corpus UNTOUCHED (not half-migrated)."""
    p = tmp_path / "old.db"
    _old_shape_db(p)

    # force a failure AFTER the binding rename/rebuild would have run
    monkeypatch.setattr(
        perm_model_rev6, "_repair_supersedes",
        lambda conn: (_ for _ in ()).throw(RuntimeError("forced mid-migration failure")),
    )
    with pytest.raises(RuntimeError):
        perm_model_rev6.migrate(p)

    conn = sqlite3.connect(str(p))
    # nothing committed: role still 'author', no CHECK, no pair columns, no grants table
    assert conn.execute("SELECT role FROM account_bindings WHERE subject='alice'").fetchone()[0] == "author"
    slug_cols = {r[1] for r in conn.execute("PRAGMA table_info(station_slugs)")}
    assert "claimed_by" in slug_cols and "claimed_by_issuer" not in slug_cols
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "document_grants" not in tables
    assert "account_bindings_new" not in tables  # no orphan scratch table
    conn.close()


def test_migration_reruns_cleanly_after_failure(tmp_path, monkeypatch):
    """After a forced failure, a clean re-run completes (no orphan *_new blocks it)."""
    p = tmp_path / "old.db"
    _old_shape_db(p)
    monkeypatch.setattr(
        perm_model_rev6, "_repair_supersedes",
        lambda conn: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        perm_model_rev6.migrate(p)
    monkeypatch.undo()  # remove the fault
    perm_model_rev6.migrate(p)  # must not raise (no orphan invites_new etc.)
    conn = sqlite3.connect(str(p))
    roles = {r[0] for r in conn.execute("SELECT role FROM account_bindings")}
    assert roles == {"operator", "originator"}
    conn.close()
