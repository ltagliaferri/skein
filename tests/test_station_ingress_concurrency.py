"""Concurrent-writer correctness for the ingress write path.

Re-homed from skein_next/tests/test_ingress_concurrency.py (station re-home Stage 3), over
the re-homed ``skein.ingress`` + ``StationStore``. The ingress opens a SQLite connection
per request and runs ingest in a threadpool, so concurrent publishes overlap. With a
DEFERRED transaction the read-then-write pattern (get_folio -> create_folio) deadlocks on
the shared->reserved lock upgrade and SQLite returns 'database is locked' instantly, which
the per-item handler would record as 'invalid fields' — silently dropping valid concurrent
publishes. transaction() uses BEGIN IMMEDIATE + a non-zero busy_timeout so writers
serialize and all valid writes commit.
"""

from __future__ import annotations

import concurrent.futures as cf
import sqlite3

import pytest

from skein.station import Station
from skein.station_store import StationStore
from skein import wire

from tests import station_publish_helpers as h


def _make_folios(n):
    """``n`` distinct, valid wire folios (built directly, no authoring verbs)."""
    return [
        h.folio("finding", f"Title {i}", f"body number {i}", f"2026-03-01T00:00:{i:02d}+00:00")
        for i in range(n)
    ]


def test_concurrent_writers_all_commit(tmp_path):
    """30 threaded writers each ingest one folio: every one commits and none is
    mislabeled a reject."""
    n = 30
    folios = _make_folios(n)
    inst = tmp_path / "inst" / ".skein-next"
    Station(inst).close()  # materialize

    def writer(f):
        st = Station(inst, check_same_thread=False)
        try:
            return ingest_one(st, f)
        finally:
            st.close()

    def ingest_one(st, f):
        from skein.ingress import ingest
        return ingest(
            st,
            {"protocol": wire.PROTOCOL, "folios": [f], "threads": [], "site_slugs": {}},
            require_signed=False,
        )

    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        acks = list(ex.map(writer, folios))

    accepted = sum(len(a["accepted"]) for a in acks)
    rejected = [r for a in acks for r in a["rejected"]]
    assert accepted == n, f"expected all {n} to commit, got {accepted}; rejects={rejected}"
    assert rejected == []

    chk = Station(inst, check_same_thread=False)
    try:
        persisted = sum(1 for f in folios if chk.store.get_folio(f["content_hash"]) is not None)
    finally:
        chk.close()
    assert persisted == n


def test_failed_begin_immediate_not_wedged(tmp_path):
    # If BEGIN IMMEDIATE times out (another connection holds the write lock longer
    # than busy_timeout), transaction() raises but must leave the store CLEAN —
    # _in_batch must NOT stay True (which would skip later commits and falsely trip
    # the not-re-entrant guard). The store must be usable once the lock frees.
    d = tmp_path / "s" / ".skein-next"
    StationStore(d).close()  # materialize

    holder = StationStore(d, check_same_thread=False)
    victim = StationStore(d, check_same_thread=False)
    victim.conn.execute("PRAGMA busy_timeout=50")  # expire the wait fast

    holder.conn.execute("BEGIN IMMEDIATE")  # hold the write lock
    try:
        with pytest.raises(sqlite3.OperationalError):
            with victim.transaction():
                pass  # never reached — BEGIN IMMEDIATE fails to take the lock
        assert victim._in_batch is False  # not wedged
    finally:
        holder.conn.rollback()
        holder.close()

    # lock is free now: the victim store re-enters transaction() cleanly
    with victim.transaction():
        pass
    victim.close()
