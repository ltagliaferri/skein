"""Stage 1b — station folio/thread/alias accessors, differential-shadowed vs skein_next.

Each accessor on ``StationStore`` (over the shared ``versions``/post-swap ``threads``
tables) must return the SAME shape/ordering as ``SkeinNextStore`` on the identical
corpus. We build the corpus in BOTH stores and assert equal results, so a drift in the
port fails loudly. Content hashes agree because skein.identity == skein_next.identity, so
the same fields address to the same hash in both stores.

Also pins the strict-null narrowing (station requires non-null structural canonical
fields — see skein/station_store.py) and thread content-address dedup on the post-swap DDL.
"""
import shutil
import tempfile
from pathlib import Path

import pytest

from skein.station_store import StationStore
from skein_next.store import SkeinNextStore


# ── corpus ──────────────────────────────────────────────────────────────────────

# Folio field dicts (no content_hash — recomputed on insert). Distinct created_at for
# deterministic ordering; f_finding and f_note SHARE a created_at to exercise the
# content_hash tiebreak. Literal '50%' and 'a_b' exercise _like_escape.
FOLIOS = {
    "site": {"type": "site", "title": "Alpha Site", "content": "the alpha site",
             "created_at": "2026-01-01T00:00:00+00:00", "created_by": "agent-1"},
    "site2": {"type": "site", "title": "Beta Site", "content": "beta home",
              "created_at": "2026-01-04T00:00:00+00:00", "created_by": "agent-1"},
    "issue": {"type": "issue", "title": "Login bug", "content": "users cannot login, 50% success",
              "created_at": "2026-01-02T00:00:00+00:00", "created_by": "agent-2"},
    "finding": {"type": "finding", "title": "login race condition", "content": "a_b race in login",
                "created_at": "2026-01-03T00:00:00+00:00", "created_by": "agent-2"},
    "note": {"type": "notion", "title": "an idea", "content": "login could be faster",
             "created_at": "2026-01-03T00:00:00+00:00", "created_by": "agent-3"},
    "cross": {"type": "issue", "title": "shared", "content": "member of both sites",
              "created_at": "2026-01-05T00:00:00+00:00", "created_by": "agent-4"},
}

# within (membership) + status edges. created_at is REQUIRED (strict-null); the station
# threads table forbids a null created_at. cross belongs to both sites.
THREAD_CREATED = "2026-02-01T00:00:00+00:00"


def _build(store, hashes):
    """Populate ``store`` with the corpus; return {name: content_hash}."""
    for name, fields in FOLIOS.items():
        hashes[name] = store.create_folio(fields)
    memberships = [
        ("issue", "site"), ("finding", "site"), ("note", "site"),
        ("cross", "site"), ("cross", "site2"),
    ]
    for folio, site in memberships:
        store.save_thread(from_id=hashes[folio], to_id=hashes[site],
                          type="within", created_at=THREAD_CREATED)
    # two status edges on issue (older 'open', newer 'closed') for latest-status order
    store.save_thread(from_id=hashes["issue"], to_id=hashes["issue"], type="status",
                     weaver="agent-2", created_at="2026-02-02T00:00:00+00:00", content="open")
    store.save_thread(from_id=hashes["issue"], to_id=hashes["issue"], type="status",
                     weaver="agent-2", created_at="2026-02-03T00:00:00+00:00", content="closed")
    return hashes


@pytest.fixture
def stores():
    """A (StationStore, SkeinNextStore) pair over the identical corpus, plus the shared
    name→hash map (identical in both)."""
    d = Path(tempfile.mkdtemp())
    try:
        station = StationStore(db_path=d / "station" / "skein.db")
        nxt = SkeinNextStore(d / "next")
        h_station, h_next = {}, {}
        _build(station, h_station)
        _build(nxt, h_next)
        assert h_station == h_next, "content hashes diverged between stores"
        yield station, nxt, h_station
        station.close()
        nxt.close()
    finally:
        shutil.rmtree(d)


_THREAD_WIRE_KEYS = ("thread_hash", "from_id", "to_id", "type", "weaver", "created_at", "content")


def _proj(thread_dict):
    """Project a thread dict to the 7 wire keys (drop the station's extra thread_id)."""
    if thread_dict is None:
        return None
    return {k: thread_dict.get(k) for k in _THREAD_WIRE_KEYS}


# ── folio accessors (dict equality) ─────────────────────────────────────────────

def test_get_folio_matches(stores):
    station, nxt, h = stores
    for name, ch in h.items():
        assert station.get_folio(ch) == nxt.get_folio(ch), name
    # missing → None (never {})
    assert station.get_folio("sha256::deadbeef") is None
    assert nxt.get_folio("sha256::deadbeef") is None


def test_list_folios_matches(stores):
    station, nxt, _ = stores
    assert station.list_folios() == nxt.list_folios()
    assert station.list_folios(limit=2) == nxt.list_folios(limit=2)
    assert station.list_folios(limit=2, offset=2) == nxt.list_folios(limit=2, offset=2)


@pytest.mark.parametrize("q", ["login", "login bug", "50%", "a_b", "LOGIN", "nomatch", "", "login faster"])
def test_search_folios_matches(stores, q):
    station, nxt, _ = stores
    assert station.search_folios(q) == nxt.search_folios(q)
    assert station.search_folios(q, limit=1) == nxt.search_folios(q, limit=1)


def test_find_by_prefix_matches(stores):
    station, nxt, h = stores
    # a real folio's own hash prefix, plus the framed algo prefix
    pfx = h["issue"][: len("sha256::") + 6]
    assert station.find_by_prefix(pfx) == nxt.find_by_prefix(pfx)
    assert station.find_by_prefix("sha256::") == nxt.find_by_prefix("sha256::")
    assert station.find_by_prefix("sha256::", limit=2) == nxt.find_by_prefix("sha256::", limit=2)


def test_folios_in_site_matches(stores):
    station, nxt, h = stores
    assert station.folios_in_site(h["site"]) == nxt.folios_in_site(h["site"])
    assert station.folios_in_site(h["site2"]) == nxt.folios_in_site(h["site2"])
    # type filter + limit params (present but rarely driven)
    assert station.folios_in_site(h["site"], type="issue") == nxt.folios_in_site(h["site"], type="issue")
    assert station.folios_in_site(h["site"], limit=2) == nxt.folios_in_site(h["site"], limit=2)


def test_count_folios_matches(stores):
    station, nxt, _ = stores
    assert station.count_folios() == nxt.count_folios() == len(FOLIOS)


# ── thread accessors (7-key projection; station carries an extra thread_id) ──────

def test_get_thread_matches(stores):
    station, nxt, h = stores
    th = station.save_thread(from_id=h["note"], to_id=h["site"], type="within",
                             created_at=THREAD_CREATED)  # already exists → same hash
    assert _proj(station.get_thread(th)) == _proj(nxt.get_thread(th))
    assert station.get_thread("sha256::missing") is None


def test_get_threads_matches(stores):
    station, nxt, h = stores
    def proj_list(rows):
        return [_proj(r) for r in rows]
    assert proj_list(station.get_threads(to_id=h["site"])) == proj_list(nxt.get_threads(to_id=h["site"]))
    assert proj_list(station.get_threads(type="within")) == proj_list(nxt.get_threads(type="within"))
    assert proj_list(station.get_threads(type="status")) == proj_list(nxt.get_threads(type="status"))
    assert proj_list(station.get_threads(from_id=h["cross"])) == proj_list(nxt.get_threads(from_id=h["cross"]))
    assert proj_list(station.get_threads()) == proj_list(nxt.get_threads())


def test_thread_dedup_on_hash(stores):
    station, nxt, h = stores
    before = len(station.get_threads())
    # re-saving byte-identical wire thread → same hash, no new row
    again = station.save_thread(from_id=h["issue"], to_id=h["site"], type="within",
                                created_at=THREAD_CREATED)
    assert again in {t["thread_hash"] for t in station.get_threads()}
    assert len(station.get_threads()) == before


def test_station_thread_dict_has_thread_id(stores):
    station, _, h = stores
    th1 = station.save_thread(from_id=h["note"], to_id=h["note"], type="status",
                              weaver="a", created_at="2026-03-01T00:00:00+00:00", content="wip")
    row = station.get_thread(th1)
    assert row["thread_id"]  # audit column present, non-null
    # thread_id is generated fresh per call but is NOT in the content hash: re-saving the
    # same wire thread returns the same hash (so it dedups) and keeps the first thread_id.
    th2 = station.save_thread(from_id=h["note"], to_id=h["note"], type="status",
                              weaver="a", created_at="2026-03-01T00:00:00+00:00", content="wip")
    assert th2 == th1
    assert station.get_thread(th2)["thread_id"] == row["thread_id"]


# ── strict-null narrowing (intentional divergence from skein_next) ───────────────

def test_create_folio_rejects_null_structural_field():
    d = Path(tempfile.mkdtemp())
    try:
        station = StationStore(db_path=d / "skein.db")
        base = {"type": "issue", "title": "t", "content": "c",
                "created_at": "2026-01-01T00:00:00+00:00", "created_by": "a"}
        for field in ("type", "title", "content", "created_at", "created_by"):
            bad = dict(base, **{field: None})
            with pytest.raises(ValueError, match=field):
                station.create_folio(bad)
        # the fully-populated folio still inserts
        assert station.create_folio(base)
        station.close()
    finally:
        shutil.rmtree(d)


def test_save_thread_rejects_null_structural_field():
    d = Path(tempfile.mkdtemp())
    try:
        station = StationStore(db_path=d / "skein.db")
        ok = dict(from_id="a", to_id="b", type="within", created_at="2026-01-01T00:00:00+00:00")
        for field in ("from_id", "to_id", "type", "created_at"):
            bad = dict(ok, **{field: None})
            with pytest.raises(ValueError, match=field):
                station.save_thread(**bad)
        # weaver + content may be null
        assert station.save_thread(**ok)
        station.close()
    finally:
        shutil.rmtree(d)


# ── aliases ──────────────────────────────────────────────────────────────────────

def test_resolve_alias_matches(stores):
    station, nxt, h = stores
    # aliases are populated out-of-band (station has no set_alias); insert directly.
    station.conn.execute("INSERT INTO aliases (legacy_id, content_hash) VALUES (?, ?)",
                         ("issue-20260101-aaaa", h["issue"]))
    station.conn.commit()
    nxt.set_alias("issue-20260101-aaaa", h["issue"])
    assert station.resolve_alias("issue-20260101-aaaa") == nxt.resolve_alias("issue-20260101-aaaa")
    assert station.resolve_alias("nope") is None and nxt.resolve_alias("nope") is None


# ── read-only open ─────────────────────────────────────────────────────────────

def test_read_only_open_does_no_ddl_and_reads():
    d = Path(tempfile.mkdtemp())
    try:
        dbp = d / "skein.db"
        w = StationStore(db_path=dbp)
        ch = w.create_folio(FOLIOS["issue"])
        w.close()
        ro = StationStore(db_path=dbp, read_only=True)
        assert ro.get_folio(ch)["title"] == "Login bug"
        ro.close()
        # read-only open on a nonexistent corpus must fail AND leave no file behind (the
        # immutable=1 fallback URI would otherwise CREATE a 0-byte skein.db).
        with pytest.raises(Exception):
            StationStore(db_path=d / "absent.db", read_only=True)
        assert not (d / "absent.db").exists()
    finally:
        shutil.rmtree(d)


def test_read_only_open_with_special_char_in_path():
    # A '#' or '?' in the path (ticket / versioned dir names) must not break the file: URI.
    d = Path(tempfile.mkdtemp())
    try:
        dbp = d / "tick#1?v2" / "skein.db"
        w = StationStore(db_path=dbp)
        ch = w.create_folio(FOLIOS["issue"])
        w.close()
        ro = StationStore(db_path=dbp, read_only=True)
        assert ro.get_folio(ch) is not None
        ro.close()
    finally:
        shutil.rmtree(d)


def _tables_of(path):
    import sqlite3 as s
    conn = s.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_rejects_workbench_db_without_altering_it():
    # A workbench (pre-swap) db at the station's default filename must be refused BEFORE any
    # station DDL runs — the refused db's schema must be untouched (no station tables bolted
    # on), not just the construction failing after the damage.
    d = Path(tempfile.mkdtemp())
    try:
        from skein.storage import LogDatabase
        p = d / "skein.db"
        LogDatabase(p)  # a workbench db (pre-swap threads, thread_id PK)
        before = _tables_of(p)
        with pytest.raises(ValueError, match="non-station db"):
            StationStore(db_path=p)
        assert _tables_of(p) == before  # NOT corrupted: no station_slugs/manifests/etc added
    finally:
        shutil.rmtree(d)


def test_rejects_migrated_workbench_db():
    # A workbench db migrated to a thread_hash PK (threads_pk_swap) has the SAME PK shape
    # as a station but no station_slugs — the discriminator must still refuse it, unaltered.
    d = Path(tempfile.mkdtemp())
    try:
        from skein.storage import LogDatabase
        from skein.migrations import threads_pk_swap
        p = d / "skein.db"
        LogDatabase(p)
        threads_pk_swap.migrate_db(p)
        before = _tables_of(p)
        with pytest.raises(ValueError, match="non-station db"):
            StationStore(db_path=p)
        assert _tables_of(p) == before
    finally:
        shutil.rmtree(d)


def test_rejects_spoofed_station_slugs_marker():
    # A db with a MALFORMED station_slugs table (wrong shape) must be refused BEFORE any
    # DDL — the marker is checked by shape (slug + anchor_hash), not mere presence, so a
    # corrupt/spoofed marker can't fool the guard into mutating the db.
    d = Path(tempfile.mkdtemp())
    try:
        import sqlite3 as s
        p = d / "skein.db"
        conn = s.connect(p)
        conn.execute("CREATE TABLE station_slugs (slug TEXT)")  # missing anchor_hash
        conn.execute("CREATE TABLE junk (x TEXT)")
        conn.commit()
        conn.close()
        before = _tables_of(p)
        with pytest.raises(ValueError, match="non-station db"):
            StationStore(db_path=p)
        assert _tables_of(p) == before  # not mutated
    finally:
        shutil.rmtree(d)


def test_station_logdatabase_connection_keeps_rollback_journal():
    # The journal-mode fix must hold on EVERY connection, not just schema birth: a
    # station-mode LogDatabase method (any _get_connection) must not flip the corpus to WAL.
    d = Path(tempfile.mkdtemp())
    try:
        import sqlite3 as s
        from skein.storage import LogDatabase
        p = d / "st" / "skein.db"
        StationStore(db_path=p).close()
        assert s.connect(p).execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        db = LogDatabase(p, station=True)
        with db._get_connection() as c:
            c.execute("SELECT 1 FROM versions LIMIT 1")
        assert s.connect(p).execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        # regression guard: a workbench db still uses WAL
        LogDatabase(d / "wb.db")
        assert s.connect(d / "wb.db").execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        shutil.rmtree(d)


def test_second_writer_while_first_open_does_not_crash():
    # Two StationStore(write) on the same corpus (e.g. concurrent ingress workers): the
    # second construction must NOT 'database is locked' on a journal-mode flip.
    d = Path(tempfile.mkdtemp())
    try:
        p = d / "skein.db"
        a = StationStore(db_path=p)
        a.create_folio(FOLIOS["issue"])
        b = StationStore(db_path=p)  # first still open
        assert b.get_folio(a.create_folio(FOLIOS["finding"])) is not None
        a.close()
        b.close()
    finally:
        shutil.rmtree(d)


def test_savepoint_requires_transaction():
    # savepoint() outside transaction() would silently commit; it must fail loudly.
    d = Path(tempfile.mkdtemp())
    try:
        s = StationStore(db_path=d / "skein.db")
        with pytest.raises(RuntimeError, match="transaction"):
            with s.savepoint():
                pass
        # inside a transaction it works, and rolls back only its own item
        before = s.count_folios()
        with pytest.raises(RuntimeError):
            with s.transaction():
                with s.savepoint():
                    s.create_folio(FOLIOS["issue"])
                    raise RuntimeError("boom")
        assert s.count_folios() == before  # the item rolled back
        s.close()
    finally:
        shutil.rmtree(d)


# ── 1c: latest_statuses (control) ────────────────────────────────────────────────

def test_latest_statuses_matches(stores):
    station, nxt, h = stores
    ids = [h["issue"], h["finding"], h["note"]]
    # issue has two status edges (open→closed); newest wins in both stores
    assert station.latest_statuses(ids) == nxt.latest_statuses(ids)
    assert station.latest_statuses(ids).get(h["issue"]) == "closed"


def test_latest_statuses_empty_and_never_open(stores):
    station, nxt, h = stores
    assert station.latest_statuses([]) == {} == nxt.latest_statuses([])
    # a folio with no status thread is ABSENT, never defaulted to 'open'
    got = station.latest_statuses([h["finding"]])
    assert h["finding"] not in got
    assert got == nxt.latest_statuses([h["finding"]])


# ── 1c: slugs (degenerate site case is skein_next-equivalent) ────────────────────

def _set_site_slugs(store, h):
    store.set_slug("alpha", h["site"])
    store.set_slug("beta", h["site2"])


def test_set_resolve_list_slugs_matches(stores):
    station, nxt, h = stores
    _set_site_slugs(station, h)
    _set_site_slugs(nxt, h)
    assert station.resolve_slug("alpha") == nxt.resolve_slug("alpha") == h["site"]
    assert station.resolve_slug("beta") == nxt.resolve_slug("beta") == h["site2"]
    assert station.resolve_slug("absent") is None and nxt.resolve_slug("absent") is None
    assert station.list_slugs() == nxt.list_slugs()
    # last-write-wins re-bind
    station.set_slug("alpha", h["site2"])
    nxt.set_slug("alpha", h["site2"])
    assert station.resolve_slug("alpha") == nxt.resolve_slug("alpha") == h["site2"]


def test_folio_site_slug_matches(stores):
    station, nxt, h = stores
    _set_site_slugs(station, h)
    _set_site_slugs(nxt, h)
    for name in ("issue", "finding", "note", "cross"):
        assert station.folio_site_slug(h[name]) == nxt.folio_site_slug(h[name]), name
    # cross is in both sites → alphabetically-first slug ('alpha')
    assert station.folio_site_slug(h["cross"]) == "alpha"


def test_folio_site_slugs_matches(stores):
    station, nxt, h = stores
    _set_site_slugs(station, h)
    _set_site_slugs(nxt, h)
    assert station.folio_site_slugs() == nxt.folio_site_slugs()
    subset = [h["issue"], h["cross"]]
    assert station.folio_site_slugs(subset) == nxt.folio_site_slugs(subset)


# ── 1c: derived-head resolution (station-only — skein_next's flat slugs can't do this) ──

def _lineage_station():
    d = Path(tempfile.mkdtemp())
    return d, StationStore(db_path=d / "skein.db")


def test_derived_head_follows_republish():
    d, station = _lineage_station()
    try:
        v1 = station.create_folio({"type": "issue", "title": "v1", "content": "first",
                                   "created_at": "2026-01-01T00:00:00+00:00", "created_by": "a"})
        station.set_slug("lin", v1)  # anchor at the genesis
        assert station.resolve_slug("lin") == v1
        # republish v2 + a supersedes edge (from_id=new, to_id=old)
        v2 = station.create_folio({"type": "issue", "title": "v2", "content": "second",
                                   "created_at": "2026-01-02T00:00:00+00:00", "created_by": "a"})
        station.save_thread(from_id=v2, to_id=v1, type="supersedes", weaver="a",
                           created_at="2026-01-02T00:00:01+00:00")
        # resolution follows the graph forward with zero mutable state
        assert station.resolve_slug("lin") == v2
        assert station.resolve_slug_heads("lin") == [v2]
    finally:
        station.close()
        shutil.rmtree(d)


def test_fork_resolves_to_fork_not_winner():
    d, station = _lineage_station()
    try:
        v1 = station.create_folio({"type": "issue", "title": "v1", "content": "first",
                                   "created_at": "2026-01-01T00:00:00+00:00", "created_by": "a"})
        station.set_slug("lin", v1)
        # two signed supersedes children of v1 → a fork
        v2a = station.create_folio({"type": "issue", "title": "v2a", "content": "branch a",
                                    "created_at": "2026-01-02T00:00:00+00:00", "created_by": "a"})
        v2b = station.create_folio({"type": "issue", "title": "v2b", "content": "branch b",
                                    "created_at": "2026-01-02T00:00:00+00:00", "created_by": "b"})
        station.save_thread(from_id=v2a, to_id=v1, type="supersedes", weaver="a",
                           created_at="2026-01-02T00:00:01+00:00")
        station.save_thread(from_id=v2b, to_id=v1, type="supersedes", weaver="b",
                           created_at="2026-01-02T00:00:02+00:00")
        assert station.resolve_slug_heads("lin") == sorted([v2a, v2b])
        # the flat contract never picks a winner from a fork
        assert station.resolve_slug("lin") is None
        # a forked slug is omitted from the catalog rather than named to one branch
        assert station.list_slugs() == []
    finally:
        station.close()
        shutil.rmtree(d)
