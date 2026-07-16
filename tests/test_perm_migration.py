"""Permission model — the one-shot rev-6 migration (rev 6 §3.1/§4/§5.3).

Transforms a PRE-rev6 station corpus (author role, single claimed_by, no grants, no
supersedes invariant) to the rev-6 shape, atomically and idempotently, and quarantines
pre-existing dangling / merge supersedes so the <=1-parent index can be built.
"""

from __future__ import annotations

import sqlite3

import pytest

from skein.migrations.perm_model_rev6 import migrate
from skein.storage import LogDatabase


def _old_shape_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE versions (content_hash TEXT PRIMARY KEY, type TEXT, title TEXT,
            content TEXT, created_at TEXT, created_by TEXT);
        CREATE TABLE threads (thread_hash TEXT PRIMARY KEY, thread_id TEXT, from_id TEXT,
            to_id TEXT, type TEXT, weaver TEXT, created_at TEXT, content TEXT);
        CREATE TABLE account_bindings (issuer TEXT, subject TEXT, role TEXT,
            vouched_by_issuer TEXT, vouched_by_subject TEXT, created_at TEXT,
            revoked_at TEXT, PRIMARY KEY (issuer, subject));
        CREATE TABLE invites (token_hash TEXT PRIMARY KEY, role TEXT NOT NULL,
            created_at TEXT, expires_at TEXT NOT NULL, used_at TEXT, revoked_at TEXT,
            vouched_by_issuer TEXT, vouched_by_subject TEXT, bound_issuer TEXT,
            bound_subject TEXT, redeemed_at TEXT, note TEXT,
            failed_attempts INTEGER NOT NULL DEFAULT 0, attempts_window_start TEXT);
        CREATE TABLE station_slugs (slug TEXT PRIMARY KEY, anchor_hash TEXT NOT NULL,
            claimed_by TEXT, scope TEXT);
        """
    )
    conn.execute("INSERT INTO account_bindings VALUES ('i','op','operator',NULL,NULL,'t',NULL)")
    conn.execute("INSERT INTO account_bindings VALUES ('i','alice','author','i','op','t',NULL)")
    conn.execute(
        "INSERT INTO invites VALUES ('th1','author','t','2099-01-01',NULL,NULL,'i','op',"
        "NULL,NULL,NULL,'n',0,NULL)"
    )
    conn.execute("INSERT INTO station_slugs VALUES ('specs','sha256::aa',NULL,NULL)")
    for h in ("g", "v2", "p1", "p2", "child", "cleankid"):
        conn.execute(
            "INSERT INTO versions VALUES (?,?,?,?,?,?)", (h, "finding", "t", "b", "t", "t")
        )
    # a DANGLING supersedes (parent 'ghost' not held)
    conn.execute("INSERT INTO threads VALUES ('t-dang','1','v2','ghost','supersedes',NULL,'t',NULL)")
    # a MERGE (child has two held parents)
    conn.execute("INSERT INTO threads VALUES ('t-m1','2','child','p1','supersedes',NULL,'t',NULL)")
    conn.execute("INSERT INTO threads VALUES ('t-m2','3','child','p2','supersedes',NULL,'t',NULL)")
    # a CLEAN supersedes (survives)
    conn.execute("INSERT INTO threads VALUES ('t-ok','4','cleankid','g','supersedes',NULL,'t',NULL)")
    conn.commit()
    conn.close()


@pytest.fixture
def old_db(tmp_path):
    p = tmp_path / "old-station.db"
    _old_shape_db(p)
    return p


def test_roles_renamed(old_db):
    migrate(old_db)
    conn = sqlite3.connect(str(old_db))
    roles = {r[0] for r in conn.execute("SELECT role FROM account_bindings")}
    assert roles == {"operator", "originator"}
    assert conn.execute("SELECT role FROM invites").fetchone()[0] == "originator"
    conn.close()


def test_role_check_installed(old_db):
    migrate(old_db)
    conn = sqlite3.connect(str(old_db))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO account_bindings VALUES ('i','x','author',NULL,NULL,'t',NULL)")
    conn.close()


def test_slug_pair_columns(old_db):
    migrate(old_db)
    conn = sqlite3.connect(str(old_db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(station_slugs)")}
    assert "claimed_by" not in cols
    assert {"claimed_by_issuer", "claimed_by_subject"} <= cols
    # the pre-existing claim is preserved (anchor kept, pair NULL)
    row = conn.execute("SELECT anchor_hash, claimed_by_issuer FROM station_slugs").fetchone()
    assert row[0] == "sha256::aa" and row[1] is None
    conn.close()


def test_grant_tables_created(old_db):
    migrate(old_db)
    conn = sqlite3.connect(str(old_db))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"document_grants", "grant_events"} <= tables
    conn.close()


def test_dangling_and_merge_quarantined(old_db):
    report = migrate(old_db)
    conn = sqlite3.connect(str(old_db))
    remaining = {r[0] for r in conn.execute("SELECT thread_hash FROM threads WHERE type='supersedes'")}
    # t-dang removed; exactly one of the two merge parents removed; clean survives
    assert "t-dang" not in remaining
    assert "t-ok" in remaining
    child_parents = conn.execute(
        "SELECT COUNT(*) FROM threads WHERE type='supersedes' AND from_id='child'"
    ).fetchone()[0]
    assert child_parents == 1
    q = {r[0] for r in conn.execute("SELECT thread_hash FROM quarantined_supersedes")}
    assert "t-dang" in q and len(q) == 2  # dangling + one merge parent
    assert report["dangling"] == 1 and report["merges"] == 1 and report["quarantined"] == 2
    conn.close()


def test_one_parent_index_enforced_after_migration(old_db):
    migrate(old_db)
    conn = sqlite3.connect(str(old_db))
    # cleankid already supersedes g; a second parent must be refused by the index
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO threads VALUES ('t-x','9','cleankid','p1','supersedes',NULL,'t',NULL)")
    conn.close()


def test_idempotent_second_run(old_db):
    migrate(old_db)
    report2 = migrate(old_db)  # no-op second pass
    assert report2["bindings_renamed"] == 0
    assert report2["slugs_rebuilt"] is False
    assert report2["quarantined"] == 0


def test_dangling_quarantine_survives_null_content_hash(tmp_path):
    """fell-r2: a NULL versions.content_hash must not make the NOT-IN dangling query
    miss real dangling rows (SQL three-valued logic). SQLite permits NULL in a TEXT
    PRIMARY KEY, so a legacy/corrupt corpus can carry one."""
    p = tmp_path / "old-null.db"
    _old_shape_db(p)
    conn = sqlite3.connect(str(p))
    # a corrupt legacy row with a NULL content_hash
    conn.execute("INSERT INTO versions VALUES (NULL,'finding','t','b','t','t')")
    conn.commit()
    conn.close()
    migrate(p)
    conn = sqlite3.connect(str(p))
    # t-dang (v2 -> ghost, ghost unheld) is still quarantined despite the NULL row
    remaining = {r[0] for r in conn.execute("SELECT thread_hash FROM threads WHERE type='supersedes'")}
    assert "t-dang" not in remaining
    q = {r[0] for r in conn.execute("SELECT thread_hash FROM quarantined_supersedes")}
    assert "t-dang" in q
    conn.close()


def test_self_edge_supersedes_quarantined(tmp_path):
    """fell-r3: a self-edge supersedes A->A (which lineage_genesis_for rejects forever)
    must be quarantined so its lineage stays resolvable."""
    p = tmp_path / "old-self.db"
    _old_shape_db(p)
    conn = sqlite3.connect(str(p))
    conn.execute("INSERT INTO threads VALUES ('t-self','9','cleankid','cleankid','supersedes',NULL,'t',NULL)")
    conn.commit()
    conn.close()
    report = migrate(p)
    conn = sqlite3.connect(str(p))
    remaining = {r[0] for r in conn.execute("SELECT thread_hash FROM threads WHERE type='supersedes'")}
    assert "t-self" not in remaining
    q = {r[0] for r in conn.execute("SELECT thread_hash FROM quarantined_supersedes")}
    assert "t-self" in q and report["self_edges"] == 1
    conn.close()


def test_null_to_id_supersedes_quarantined(tmp_path):
    """fell-r3: a supersedes with a NULL to_id is dangling but NOT IN is NULL-blind on
    the row side too — quarantine must still catch it so the <=1-parent index builds."""
    p = tmp_path / "old-nullto.db"
    _old_shape_db(p)
    conn = sqlite3.connect(str(p))
    conn.execute("INSERT INTO threads VALUES ('t-nullto','9','cleankid2',NULL,'supersedes',NULL,'t',NULL)")
    conn.commit()
    conn.close()
    migrate(p)
    conn = sqlite3.connect(str(p))
    remaining = {r[0] for r in conn.execute("SELECT thread_hash FROM threads WHERE type='supersedes'")}
    assert "t-nullto" not in remaining
    q = {r[0] for r in conn.execute("SELECT thread_hash FROM quarantined_supersedes")}
    assert "t-nullto" in q
    conn.close()


def test_orphan_child_supersedes_quarantined(tmp_path):
    """audit-cap: a supersedes from an UNHELD child to a HELD parent survives the
    dangling sweep (which only tests to_id) — and when that child hash is later imported
    it silently redirects the parent's slug with no authorization. Quarantine it."""
    p = tmp_path / "old-orphan.db"
    _old_shape_db(p)
    conn = sqlite3.connect(str(p))
    # from_id 'ghostchild' is NOT held; to_id 'g' IS held -> not dangling, but an orphan
    conn.execute("INSERT INTO threads VALUES ('t-orphan','9','ghostchild','g','supersedes',NULL,'t',NULL)")
    conn.commit()
    conn.close()
    report = migrate(p)
    conn = sqlite3.connect(str(p))
    remaining = {r[0] for r in conn.execute("SELECT thread_hash FROM threads WHERE type='supersedes'")}
    assert "t-orphan" not in remaining
    q = {r[0] for r in conn.execute("SELECT thread_hash FROM quarantined_supersedes")}
    assert "t-orphan" in q and report["orphan_children"] >= 1
    conn.close()


def test_cycle_supersedes_broken(tmp_path):
    """audit-cap: a two-row cycle (A->B, B->A) passes the merge sweep and the <=1-parent
    index, but makes lineage_genesis_for fail-closed FOREVER — the lineage is permanently
    unauthorizable and its slug unresolvable. The migration must break it."""
    p = tmp_path / "old-cycle.db"
    _old_shape_db(p)
    conn = sqlite3.connect(str(p))
    conn.execute("INSERT INTO versions VALUES ('ca','finding','t','b','t','t')")
    conn.execute("INSERT INTO versions VALUES ('cb','finding','t','b','t','t')")
    conn.execute("INSERT INTO threads VALUES ('t-c1','9','ca','cb','supersedes',NULL,'t',NULL)")
    conn.execute("INSERT INTO threads VALUES ('t-c2','9','cb','ca','supersedes',NULL,'t',NULL)")
    conn.commit()
    conn.close()
    report = migrate(p)
    assert report["cycles_broken"] >= 1
    conn = sqlite3.connect(str(p))
    # the loop is broken: exactly one of the two edges survives, the other is quarantined
    left = {r[0] for r in conn.execute(
        "SELECT thread_hash FROM threads WHERE thread_hash IN ('t-c1','t-c2')")}
    assert len(left) == 1
    q = {r[0] for r in conn.execute(
        "SELECT thread_hash FROM quarantined_supersedes WHERE quarantined_reason='cycle_edge'")}
    assert len(q) == 1
    # no from_id in the surviving graph still loops back on itself
    rows = dict(conn.execute("SELECT from_id, to_id FROM threads WHERE type='supersedes'").fetchall())
    assert not (rows.get("ca") == "cb" and rows.get("cb") == "ca")
    conn.close()


def test_null_thread_hash_row_actually_removed(tmp_path):
    """audit-cap r2: SQLite permits NULL in a TEXT PRIMARY KEY, so a DELETE keyed on
    thread_hash removes ZERO rows for a NULL-hash row — which would leave it LIVE while
    the report claims it quarantined, and make the cycle loop spin forever. Quarantine
    deletes by ROWID, so the row genuinely goes."""
    p = tmp_path / "old-nullhash.db"
    _old_shape_db(p)
    conn = sqlite3.connect(str(p))
    # a NULL-thread_hash dangling row (unheld parent)
    conn.execute("INSERT INTO threads VALUES (NULL,'9','nh-child','ghost','supersedes',NULL,'t',NULL)")
    conn.commit()
    conn.close()
    migrate(p)
    conn = sqlite3.connect(str(p))
    live = conn.execute(
        "SELECT COUNT(*) FROM threads WHERE type='supersedes' AND from_id='nh-child'"
    ).fetchone()[0]
    assert live == 0  # genuinely removed, not just reported
    conn.close()


def test_null_hash_cycle_terminates(tmp_path):
    """audit-cap r2: a cycle whose victim edge has a NULL thread_hash must still be
    broken — with a thread_hash-keyed DELETE the repair loop never terminates."""
    p = tmp_path / "old-nullcycle.db"
    _old_shape_db(p)
    conn = sqlite3.connect(str(p))
    conn.execute("INSERT INTO versions VALUES ('na','finding','t','b','t','t')")
    conn.execute("INSERT INTO versions VALUES ('nb','finding','t','b','t','t')")
    conn.execute("INSERT INTO threads VALUES (NULL,'9','na','nb','supersedes',NULL,'t',NULL)")
    conn.execute("INSERT INTO threads VALUES ('t-nb','9','nb','na','supersedes',NULL,'t',NULL)")
    conn.commit()
    conn.close()
    report = migrate(p)  # must TERMINATE
    assert report["cycles_broken"] >= 1
    conn = sqlite3.connect(str(p))
    rows = dict(conn.execute(
        "SELECT from_id, to_id FROM threads WHERE type='supersedes' AND from_id IN ('na','nb')"
    ).fetchall())
    assert not (rows.get("na") == "nb" and rows.get("nb") == "na")  # loop broken
    conn.close()


def test_find_cycle_is_linear_on_a_long_chain(tmp_path):
    """audit-cap r2: _find_cycle ran O(n^3) (list scan per start, no memoization) — a
    long revision chain stalled the migration for minutes under BEGIN IMMEDIATE. A
    2000-node cycle-free chain must resolve fast."""
    import time
    from skein.migrations.perm_model_rev6 import _find_cycle
    edges = {f"v{i}": (f"v{i+1}", i, f"h{i}", "t") for i in range(2000)}
    start = time.monotonic()
    assert _find_cycle(edges) is None
    assert time.monotonic() - start < 2.0  # was ~28s at n=2000


def test_noop_on_fresh_rev6_db(tmp_path):
    """A corpus born in the rev-6 shape migrates cleanly as a no-op."""
    p = tmp_path / "fresh.db"
    LogDatabase(p, station=True)
    report = migrate(p)
    assert report["bindings_renamed"] == 0 and report["slugs_rebuilt"] is False
    conn = sqlite3.connect(str(p))
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_threads_supersedes_one_parent'"
    ).fetchone()
    assert idx is not None
    conn.close()
