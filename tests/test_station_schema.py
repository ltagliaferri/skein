"""Stage 1a — station schema + threads DDL (station re-home).

A station LogDatabase (station=True) is born with the federation + station_slugs
sidecar tables and the POST-swap threads DDL (thread_hash PK). A workbench
LogDatabase (station=False, the default) is byte-identical to before: none of the
station-only tables, pre-swap threads (thread_id PK). See
docs/STAGE_1_STORE_UNIFICATION.md.
"""
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from skein.storage import LogDatabase


STATION_TABLES = {
    "station_slugs",
    "aliases",
    "manifests",
    "constituent_attribution",
    "account_bindings",
    "binding_events",
    "invites",
    "invite_events",
    "verify_cache",
}


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d)


def _tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def _pk_cols(db_path, table):
    """Ordered list of PRIMARY KEY column names for a table."""
    conn = sqlite3.connect(db_path)
    try:
        rows = list(conn.execute(f"PRAGMA table_info({table})"))
    finally:
        conn.close()
    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk); pk>0 marks
    # membership + order in the key.
    pk = sorted((r[5], r[1]) for r in rows if r[5] > 0)
    return [name for _, name in pk]


def _col_notnull(db_path, table, col):
    conn = sqlite3.connect(db_path)
    try:
        rows = list(conn.execute(f"PRAGMA table_info({table})"))
    finally:
        conn.close()
    for r in rows:
        if r[1] == col:
            return bool(r[3])
    raise AssertionError(f"{table}.{col} not found")


# ── station db ────────────────────────────────────────────────────────────────

def test_station_db_has_all_sidecar_tables(tmp_dir):
    p = tmp_dir / "station.db"
    LogDatabase(p, station=True)
    tables = _tables(p)
    missing = STATION_TABLES - tables
    assert not missing, f"station db missing sidecar tables: {missing}"


def test_station_db_shares_versions(tmp_dir):
    p = tmp_dir / "station.db"
    LogDatabase(p, station=True)
    assert "versions" in _tables(p)


def test_station_threads_is_post_swap(tmp_dir):
    p = tmp_dir / "station.db"
    LogDatabase(p, station=True)
    assert _pk_cols(p, "threads") == ["thread_hash"]
    # thread_id survives as a NOT NULL audit column (one threads model, born migrated).
    assert _col_notnull(p, "threads", "thread_id")


def test_station_slugs_is_genesis_anchored(tmp_dir):
    p = tmp_dir / "station.db"
    LogDatabase(p, station=True)
    conn = sqlite3.connect(p)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(station_slugs)")}
    finally:
        conn.close()
    assert {"slug", "anchor_hash", "claimed_by", "scope"} <= cols


# ── workbench db (regression — must be untouched) ───────────────────────────────

def test_workbench_db_has_no_station_tables(tmp_dir):
    p = tmp_dir / "workbench.db"
    LogDatabase(p)  # default station=False
    leaked = STATION_TABLES & _tables(p)
    # station_slugs/manifests/etc must never appear on a workbench db. (aliases is
    # station-only too; a workbench db must not carry it.)
    assert not leaked, f"workbench db leaked station tables: {leaked}"


def test_workbench_threads_is_pre_swap(tmp_dir):
    p = tmp_dir / "workbench.db"
    LogDatabase(p)
    assert _pk_cols(p, "threads") == ["thread_id"]


def test_default_is_workbench(tmp_dir):
    p = tmp_dir / "default.db"
    db = LogDatabase(p)
    assert db.station is False
    assert "station_slugs" not in _tables(p)


# The exact threads DDL text a workbench db born on master (a38425f) stores in
# sqlite_master.sql. SQLite records the CREATE statement verbatim (normalizing away
# IF NOT EXISTS + leading whitespace, preserving internal indentation), so this is the
# byte-identity golden the station re-home must not disturb.
_MASTER_THREADS_SQL = (
    "CREATE TABLE threads (\n"
    "                    thread_id TEXT PRIMARY KEY,\n"
    "                    from_id TEXT NOT NULL,\n"
    "                    to_id TEXT NOT NULL,\n"
    "                    type TEXT NOT NULL,\n"
    "                    content TEXT,\n"
    "                    weaver TEXT,\n"
    "                    created_at DATETIME NOT NULL,\n"
    "                    thread_hash TEXT\n"
    "                )"
)


def test_workbench_threads_ddl_byte_equal(tmp_dir):
    """A workbench db's STORED threads DDL text is byte-identical to master's. The
    station re-home wraps the threads DDL in an if/else; this pins that the workbench
    (station=False) branch still stores the exact bytes master did — the byte-identity
    invariant docs/STAGE_1_STORE_UNIFICATION.md locks (a whitespace-only drift here is a
    regression to catch, even though it is functionally inert)."""
    p = tmp_dir / "workbench.db"
    LogDatabase(p)
    conn = sqlite3.connect(p)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'threads'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert sql == _MASTER_THREADS_SQL
