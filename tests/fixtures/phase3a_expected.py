"""Independent expected-value reducer — Leg C of the Phase 3a oracle.

This is the de-circularizing answer key. It is hand-written from the design rule
in ``docs/PHASE_3_DESIGN.md`` directly and shares **no code** with the production
A1 reader (``get_latest_statuses`` / ``get_current_status`` /
``get_latest_archives``) or the A3 refs rebuild. The whole point of Leg C is that
a reduction bug shared by the reader and the rebuild is *absent* here, so the
oracle's ``rebuilt_refs == expected`` comparison can catch it. Do not import or
mirror the production reducers in this module — that would re-introduce the
circularity (see the test-design doc §1, "Leg C").

The rule (the only thing this implements):

  A folio's current control value is the *latest* control thread anchored to that
  folio, where
    - "latest" = max by ``(created_at, thread_id)`` — the deterministic order the
      design pins in both reader and rebuild;
    - "anchored" unions the two keyspaces: a thread is anchored to a folio if its
      anchor endpoint equals the folio's slug (legacy slug-keyed) OR the folio's
      genesis hash (post-A2 genesis-keyed). I1 makes genesis unique per lineage,
      so the genesis→slug map is well defined.
    - the anchor endpoint is ``to_id`` for ``status`` / ``archive`` (self-loops)
      and ``from_id`` for ``assignment`` (``from = folio``, ``to = assignee``).
  A thread whose anchor matches neither a live slug nor a live genesis is an
  orphan (deleted folio) and contributes nothing — deleted-referent semantics.

Run it over the FROZEN PRE-A3 snapshot (the slug-keyed data, before A3 mutates
anything). A3 only re-keys; it preserves the latest value. So the per-folio value
computed here over pre-A3 data is exactly what ``refs`` must hold after the
rebuild — which is what makes it a valid answer key for the post-A3 comparison.

Value contract per column (what the rebuilt ``refs`` column must equal):
  status      -> the latest status thread's ``content``; default ``"open"``.
  assigned_to -> the latest assignment thread's ``to_id`` (the assignee); default
                 ``None``.
  archived    -> ``1`` iff the latest archive thread's ``content`` is the archived
                 marker ``"archived"``, else ``0``; default ``0``. (The A2 archive
                 writer must honor this marker; pinned here so the two agree.)
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Dict, Optional

ARCHIVED_MARKER = "archived"


def _ref_maps(conn: sqlite3.Connection) -> tuple[set[str], Dict[str, str]]:
    """(set of live slugs, genesis_hash -> slug). Genesis is unique per lineage (I1)."""
    slugs: set[str] = set()
    genesis_to_slug: Dict[str, str] = {}
    for slug, genesis in conn.execute("SELECT slug, genesis_hash FROM refs"):
        slugs.add(slug)
        if genesis is not None:
            # I1: a genesis maps to exactly one slug. If the snapshot violates it,
            # the A3 precondition refuses the db before we ever get here; defend
            # anyway so a bad snapshot is a loud failure, not a silent last-wins.
            if genesis in genesis_to_slug and genesis_to_slug[genesis] != slug:
                raise ValueError(
                    f"I1 violation in snapshot: genesis {genesis} maps to both "
                    f"{genesis_to_slug[genesis]} and {slug}"
                )
            genesis_to_slug[genesis] = slug
    return slugs, genesis_to_slug


def _latest_per_folio(
    conn: sqlite3.Connection,
    ttype: str,
    anchor_col: str,
    value_fn: Callable[[sqlite3.Row], object],
    slugs: set[str],
    genesis_to_slug: Dict[str, str],
) -> Dict[str, object]:
    """Reduce threads of ``ttype`` to one value per folio, latest by (created_at, thread_id)."""
    best_key: Dict[str, tuple] = {}
    best_val: Dict[str, object] = {}
    cur = conn.execute(
        "SELECT thread_id, from_id, to_id, content, created_at "
        "FROM threads WHERE type = ?",
        (ttype,),
    )
    cur.row_factory = sqlite3.Row
    for row in cur.fetchall():
        anchor = row[anchor_col]
        if anchor in slugs:
            slug = anchor
        elif anchor in genesis_to_slug:
            slug = genesis_to_slug[anchor]
        else:
            continue  # orphan: anchor resolves to no live folio
        # (created_at, thread_id) is a total order; ISO timestamps sort
        # lexicographically in the same order as chronologically.
        key = (row["created_at"], row["thread_id"])
        if slug not in best_key or key > best_key[slug]:
            best_key[slug] = key
            best_val[slug] = value_fn(row)
    return best_val


def expected_control(conn: sqlite3.Connection) -> Dict[str, Dict[str, object]]:
    """Per-folio expected control map: slug -> {status, assigned_to, archived}.

    Every live folio (every ``refs.slug``) appears, with defaults applied where no
    control thread covers it. Read-only.
    """
    slugs, genesis_to_slug = _ref_maps(conn)

    status = _latest_per_folio(
        conn, "status", "to_id", lambda r: r["content"], slugs, genesis_to_slug
    )
    assigned = _latest_per_folio(
        conn, "assignment", "from_id", lambda r: r["to_id"], slugs, genesis_to_slug
    )
    archive_marker = _latest_per_folio(
        conn,
        "archive",
        "to_id",
        lambda r: 1 if r["content"] == ARCHIVED_MARKER else 0,
        slugs,
        genesis_to_slug,
    )

    out: Dict[str, Dict[str, object]] = {}
    for slug in slugs:
        out[slug] = {
            "status": status.get(slug, "open"),
            "assigned_to": assigned.get(slug, None),
            "archived": int(archive_marker.get(slug, 0)),
        }
    return out


def expected_for_slug(conn: sqlite3.Connection, slug: str) -> Optional[Dict[str, object]]:
    """Convenience: the expected control triple for one folio, or None if no such ref."""
    return expected_control(conn).get(slug)
