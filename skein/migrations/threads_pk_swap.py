#!/usr/bin/env python3
"""Phase 3b — the thread_hash PK swap + Class B structural re-anchor (design §4-§8).

This is the destructive live migration that makes ``threads.thread_hash`` the
primary key, re-anchors the folio↔folio "Class B" structural edges from slugs to
content hashes, and collapses true byte-duplicates. It is written to the contract
pinned in ``tests/test_phase3b_threads.py`` and driven by ``docs/PHASE_3B_DESIGN.md``.

This module lands in increments (each felled): the per-row classifier and the
manifest-admission predicate first (pure functions, §5 / §7), then ``migrate_db``
(the table rewrite, §4.4), then the verifier (a sibling module).
"""

from __future__ import annotations

# Control types are class A by TYPE regardless of endpoints (design §3.1 / §5):
# the server mints them and they drive refs filters. Kept in sync with
# storage.CONTROL_THREAD_TYPES (imported so there is one source of truth).
from skein.storage import CONTROL_THREAD_TYPES

# Types that are never structural folio↔folio edges even when both endpoints
# happen to resolve to a folio (design §5.2): a tag is folio→label, a message is
# folio commentary. Their F→F rows are degenerate self-loops, not edges.
SEMANTIC_C_TYPES = ("tag", "message")

# The manifest allow-list (design §7): the structural edge types that may federate.
# An ALLOW-LIST, not a denylist — an unknown/new type must be rejected, never
# silently federated. `within`/`published` are named ahead of their enum addition
# (design §9 #2) so the rule is complete.
STRUCTURAL_TYPES = (
    "reference", "mention", "reply", "succession", "within", "published",
    "supersedes", "reverted",
)


class PreconditionError(RuntimeError):
    """Typed refusal: ``migrate_db`` raises this when a db fails a fail-closed
    precondition (an I1 genesis collision, a re-anchor collision that would collapse
    two DISTINCT structural edges, a non-quiesced service, or a pre-existing backup)
    — so a caller can distinguish a deliberate refusal from an incidental crash
    (design §4.4 / §5.3)."""


def classify_row(row, *, slugs, versions) -> str:
    """Classify one thread row as ``'A'`` (control), ``'B'`` (structural folio↔folio
    edge), or ``'C'`` (non-federating), per design §3.1/§5. ``row`` carries
    ``from_id``/``to_id``/``type``; ``slugs`` is the set of live ``refs.slug`` and
    ``versions`` the set of ``versions.content_hash``.

    Order matters: control wins by type (an ``assignment``'s agent ``to_id`` would
    otherwise read non-folio), then the semantic-C overrides and the self-loop and
    orphan tests demote to C; only a distinct, both-endpoints-folio, non-override row
    is B.
    """
    ttype = row["type"]
    from_id, to_id = row["from_id"], row["to_id"]

    # 1. control is class A by type, regardless of endpoints (control precedence).
    if ttype in CONTROL_THREAD_TYPES:
        return "A"
    # 2. tag / message are C even when both endpoints resolve to a folio.
    if ttype in SEMANTIC_C_TYPES:
        return "C"
    # 3. a self-loop is not a structural edge between two folios.
    if from_id == to_id:
        return "C"
    # 4. an orphan endpoint (resolves to neither a live slug nor a version) → C;
    #    kept-and-logged, never re-anchored (§5.3).
    folio_ids = slugs | versions
    if from_id not in folio_ids or to_id not in folio_ids:
        return "C"
    # 5. distinct, both-folio, non-override → structural class B.
    return "B"


def manifest_eligible(row, *, versions) -> bool:
    """The Merkle-manifest admission predicate (design §7), an explicit ALLOW-LIST.
    True iff the row is a structural type, connects two DISTINCT folios, and BOTH
    endpoints resolve in the (append-only) ``versions`` set. Class A control
    self-loops also carry version-hash endpoints, so both the type gate and the
    ``from != to`` gate are load-bearing.
    """
    if row["type"] not in STRUCTURAL_TYPES:
        return False
    from_id, to_id = row["from_id"], row["to_id"]
    if from_id == to_id:
        return False
    return from_id in versions and to_id in versions
