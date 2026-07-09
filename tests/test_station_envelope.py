"""Station envelope libs — the store-free subset of the unified wire envelope
(re-homed from skein_next/tests/test_envelope.py, station re-home Stage 2).

These cover the envelope's kind->stability->proof invariants and the derived
collection / error frames, which need no store. The folio-envelope, lineage, and
provenance-verdict tests from skein_next's test_envelope.py exercise
``build_folio_envelope`` / ``folio_verdict`` against a real station store and
ride with the read server + the federation store accessors in Stage 4 (Stage 2 is
libs + tests only; the federation accessors are not re-homed yet).
"""

from __future__ import annotations

import pytest

from skein import envelope as env_mod


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


# --- collections / entries / error ------------------------------------------


def test_folio_entry_points_at_an_address():
    row = {"content_hash": "sha256::" + "a" * 64, "type": "finding", "title": "T"}
    entry = env_mod.folio_entry(row, snippet="hi")
    assert entry["address"] == row["content_hash"]
    assert entry["kind"] == "folio"
    assert entry["type"] == "finding" and entry["title"] == "T"
    assert entry["snippet"] == "hi"
    assert entry["href"].endswith(row["content_hash"])


def test_folio_entry_defaults_type_and_title():
    row = {"content_hash": "sha256::" + "b" * 64}
    entry = env_mod.folio_entry(row)
    assert entry["type"] == "folio" and entry["title"] == ""
    assert entry["snippet"] is None


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


def test_error_envelope_without_origin_omits_link():
    env = env_mod.build_error_envelope("not_found", "sha256::" + "0" * 64)
    assert "origin" not in env["links"]
    assert env["body"] == {"found": False, "error": "not_found"}
