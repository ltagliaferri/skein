"""Permission model — the exhaustive, fail-closed thread-type classifier + drift test
(rev 6 §5.1/§8, brief-20260716-u73x).

The classifier is the source of truth for how each thread type is authorized. The
load-bearing invariant is DRIFT: its key set MUST equal the union of the code's
thread-type sets, and every literal must land in its NAMED class — so a new thread
type cannot slip into the code and reach ingress unclassified (which, fail-closed,
would be a REJECT, but the drift test surfaces it in CI as an explicit decision).
"""

from __future__ import annotations

import typing

import pytest

from skein import envelope, storage
from skein.migrations import threads_pk_swap
from skein.models import ThreadType
from skein.thread_authz import (
    ThreadClass,
    UnknownThreadType,
    classify_thread,
    known_thread_types,
    thread_reads,
)


def _code_thread_type_union() -> set:
    """The union of EVERY thread-type set the code declares — the classifier must
    cover exactly this. Assembled from the live constants (not a frozen copy), so a
    new member added to any of them makes the drift test fail here."""
    union: set = set(typing.get_args(ThreadType))
    union |= set(envelope._LINEAGE_THREADS)
    union |= set(envelope._STRUCTURAL_THREADS)
    union |= set(storage.CONTROL_THREAD_TYPES)
    union |= set(storage.CLASS_B_GENESIS_DISPLAY_TYPES)
    union |= set(threads_pk_swap.VERSION_ANCHORED_TYPES)
    return union


# The named class each real literal MUST land in (§5.1). Written out in full — NOT
# derived from the taxonomy — so this test independently pins every placement and a
# silent reclassification in the module is caught.
EXPECTED_CLASS = {
    "supersedes": ThreadClass.AFFECTING,
    "within": ThreadClass.AFFECTING,
    "reference": ThreadClass.POINTER,
    "reply": ThreadClass.POINTER,
    "mention": ThreadClass.POINTER,
    "forks": ThreadClass.POINTER,
    "responds_to": ThreadClass.POINTER,
    "imports_legacy": ThreadClass.POINTER,
    "tag": ThreadClass.POINTER,
    "status": ThreadClass.CONTROL,
    "reverted": ThreadClass.CONTROL,
    "archive": ThreadClass.CONTROL,
    "assignment": ThreadClass.ASSIGNMENT,
    "attribution": ThreadClass.ATTRIBUTION,
    "message": ThreadClass.NON_FOLIO,
    "succession": ThreadClass.NON_FOLIO,
    "published": ThreadClass.WIRE_REJECT,
}


def test_taxonomy_is_exactly_the_code_union():
    """No unclassified code type; no phantom taxonomy entry. Both directions."""
    assert known_thread_types() == _code_thread_type_union()


def test_union_is_the_expected_17():
    assert len(_code_thread_type_union()) == 17
    assert set(EXPECTED_CLASS) == _code_thread_type_union()


def test_each_literal_lands_in_its_named_class():
    for ttype, klass in EXPECTED_CLASS.items():
        assert classify_thread(ttype) == klass, ttype


def test_unknown_type_fails_closed():
    with pytest.raises(UnknownThreadType):
        classify_thread("some_new_type_nobody_classified")


def test_within_is_in_thread_type_literal():
    # within was historically missing from the Literal though live in the store.
    assert "within" in set(typing.get_args(ThreadType))


def test_tag_carries_no_read_power():
    # Audited: zero references to 'tag' across the read/render surfaces.
    assert thread_reads("tag") is False


def test_read_effect_map_is_total_over_the_union():
    # Every classified type has an answerable read-effect (no KeyError, fail-closed
    # on unknown) — co-located so a new type must declare both.
    for ttype in _code_thread_type_union():
        assert isinstance(thread_reads(ttype), bool)
    with pytest.raises(UnknownThreadType):
        thread_reads("some_new_type_nobody_classified")
