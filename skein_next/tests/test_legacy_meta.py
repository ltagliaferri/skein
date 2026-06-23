"""The legacy ``metadata`` preservation envelope (import bridge)."""

import json

from skein_next.legacy_meta import (
    LEGACY_META_MARKER,
    normalize_legacy_meta,
    parse_legacy_meta,
    render_legacy_meta,
)


def test_render_parse_round_trip():
    meta = {"confidence": 8, "reviewer": "fell-r1", "worktree_name": "shard-x"}
    content = render_legacy_meta("the tender body", meta)
    assert "the tender body" in content
    assert parse_legacy_meta(content) == meta


def test_render_is_stable_sorted_keys():
    # Same logical meta -> byte-identical content (so the imported folio hash is
    # stable and re-import is idempotent), regardless of input key order.
    a = render_legacy_meta("b", {"z": 1, "a": 2})
    b = render_legacy_meta("b", {"a": 2, "z": 1})
    assert a == b


def test_render_handles_none_body():
    content = render_legacy_meta(None, {"k": "v"})
    assert parse_legacy_meta(content) == {"k": "v"}


def test_parse_absent_returns_none():
    assert parse_legacy_meta("just a body, no envelope") is None
    assert parse_legacy_meta("") is None
    assert parse_legacy_meta(None) is None


def test_parse_ignores_decoy_marker_in_body():
    # A decoy marker (no valid fence) in the body must not fool the parser; the
    # real envelope is the last marker introducing a valid json fence.
    real = render_legacy_meta("body", {"real": True})
    poisoned = f"{LEGACY_META_MARKER} not a fence\n\n" + real
    assert parse_legacy_meta(poisoned) == {"real": True}


def test_parse_picks_last_valid_block():
    first = render_legacy_meta("body", {"which": "first"})
    both = first + "\n" + render_legacy_meta("more", {"which": "last"})
    assert parse_legacy_meta(both) == {"which": "last"}


# --- normalize_legacy_meta ---------------------------------------------------


def test_normalize_strips_dead_flag():
    assert normalize_legacy_meta('{"questions_enabled": true}') is None
    assert normalize_legacy_meta('{"questions_enabled": true, "confidence": 8}') == {
        "confidence": 8
    }


def test_normalize_empty_and_blank_return_none():
    for v in (None, "", "{}", "   ", "null"):
        assert normalize_legacy_meta(v) is None


def test_normalize_non_dict_json_is_wrapped():
    assert normalize_legacy_meta("[1, 2, 3]") == {"_value": [1, 2, 3]}
    assert normalize_legacy_meta("42") == {"_value": 42}


def test_normalize_non_json_text_preserved_raw():
    assert normalize_legacy_meta("not json at all") == {"_raw": "not json at all"}


def test_normalize_already_dict_object():
    # A dict (not a string) is accepted too — defensive against a caller passing
    # the parsed value rather than the raw cell.
    assert normalize_legacy_meta(json.dumps({"a": 1})) == {"a": 1}
