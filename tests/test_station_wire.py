"""Station publish-wire libs — unit tests for skein.wire (station re-home Stage 2).

skein_next had no standalone test_wire.py — wire.py was covered indirectly by the
e2e/ingress/publish suites (all Stage 3+). wire.py's reject-reason totality is
load-bearing at the ingress boundary (a hostile body must be a typed reject, never
a 500), so these focused units pin the re-homed module directly, ahead of the
server stages that lean on it.
"""

from __future__ import annotations

from skein import wire
from skein.identity import compute_folio_hash, compute_thread_hash


_FIELDS = {
    "type": "finding",
    "title": "T",
    "content": "body",
    "created_at": "2026-01-01T00:00:00Z",
    "created_by": "a",
}


def _intact_folio_wire(fields=_FIELDS):
    row = {**fields, "content_hash": compute_folio_hash(fields)}
    return wire.folio_to_wire(row)


def _intact_thread_wire():
    edge = {
        "from_id": "sha256::" + "a" * 64,
        "to_id": "sha256::" + "b" * 64,
        "type": "within",
        "weaver": "alice",
        "created_at": "2026-01-02T00:00:00Z",
        "content": None,
    }
    edge["thread_hash"] = compute_thread_hash(
        from_id=edge["from_id"],
        to_id=edge["to_id"],
        type=edge["type"],
        weaver=edge["weaver"],
        created_at=edge["created_at"],
        content=edge["content"],
    )
    return wire.thread_to_wire(edge)


# --- serialization shape ----------------------------------------------------


def test_folio_to_wire_is_content_hash_plus_canon_fields():
    wf = _intact_folio_wire()
    assert set(wf.keys()) == {"content_hash", *wire.FOLIO_WIRE_FIELDS}
    assert wf["type"] == "finding" and wf["title"] == "T"


def test_thread_to_wire_is_thread_hash_plus_canon_fields():
    tw = _intact_thread_wire()
    assert set(tw.keys()) == {"thread_hash", *wire.THREAD_WIRE_FIELDS}


def test_build_batch_shape():
    wf = _intact_folio_wire()
    tw = _intact_thread_wire()
    site = "sha256::" + "c" * 64
    batch = wire.build_batch(
        [{**_FIELDS, "content_hash": wf["content_hash"]}],
        [{"thread_hash": tw["thread_hash"], **{k: tw[k] for k in wire.THREAD_WIRE_FIELDS}}],
        site_slugs={site: "specs"},
    )
    assert batch["protocol"] == wire.PROTOCOL
    assert len(batch["folios"]) == 1 and len(batch["threads"]) == 1
    assert batch["site_slugs"] == {site: "specs"}


def test_build_batch_defaults_empty_site_slugs():
    assert wire.build_batch([], []) == {
        "protocol": wire.PROTOCOL,
        "folios": [],
        "threads": [],
        "site_slugs": {},
    }


# --- integrity: intact / mismatch / totality --------------------------------


def test_intact_folio_has_no_reject_reason():
    wf = _intact_folio_wire()
    assert wire.folio_reject_reason(wf) is None
    assert wire.folio_hash_ok(wf) is True


def test_folio_hash_mismatch_is_reported():
    wf = _intact_folio_wire()
    wf["title"] = "TAMPERED"  # body no longer hashes to content_hash
    assert wire.folio_reject_reason(wf) == "hash mismatch"
    assert wire.folio_hash_ok(wf) is False


def test_folio_unhashable_body_is_invalid_fields_not_raise():
    # A hostile body that trips canon (non-str title) must be a TYPED reject, never
    # an exception out of the ingress boundary (totality).
    wf = {**_FIELDS, "title": True, "content_hash": "sha256::" + "0" * 64}
    assert wire.folio_reject_reason(wf) == "invalid fields"
    assert wire.folio_hash_ok(wf) is False


def test_folio_unparseable_created_at_is_invalid_fields():
    wf = {**_FIELDS, "created_at": "not-a-date", "content_hash": "sha256::" + "0" * 64}
    assert wire.folio_reject_reason(wf) == "invalid fields"


def test_intact_thread_has_no_reject_reason():
    tw = _intact_thread_wire()
    assert wire.thread_reject_reason(tw) is None
    assert wire.thread_hash_ok(tw) is True


def test_thread_hash_mismatch_is_reported():
    tw = _intact_thread_wire()
    tw["type"] = "supersedes"  # edge no longer hashes to thread_hash
    assert wire.thread_reject_reason(tw) == "hash mismatch"
    assert wire.thread_hash_ok(tw) is False


def test_thread_unhashable_edge_is_invalid_fields_not_raise():
    tw = {
        "thread_hash": "sha256::" + "0" * 64,
        "from_id": "sha256::" + "a" * 64,
        "to_id": "sha256::" + "b" * 64,
        "type": 123,  # non-str type trips canon
        "weaver": "alice",
        "created_at": "2026-01-02T00:00:00Z",
        "content": None,
    }
    assert wire.thread_reject_reason(tw) == "invalid fields"
