"""Station folio/thread/alias accessors, pinned against the frozen skein_next oracle.

Originally a differential shadow (Stage 1b): each accessor on ``StationStore`` was
asserted equal to ``SkeinNextStore`` over an identical corpus. skein_next has since
been deleted (station re-home Stage 8); the values it produced are frozen here as
literals — captured from a live ``SkeinNextStore`` over this exact corpus while it
still existed — so every accessor is still pinned to the SAME shape/ordering/content
skein_next returned. Coverage/behavior is unchanged; only the live oracle is gone.

The ``content_hash`` values are the store's own content-addressed digests (surfaced
at runtime through the shared name→hash map); the frozen ``thread_hash`` literals are
likewise content-addressed and matched skein_next byte-for-byte because both stores
share ``skein.identity``. Each accessor return was captured to equal
``dict(FOLIOS[name], content_hash=...)`` for folios (no normalization on round-trip),
so the frozen expectations below are expressed through that shape.

Also pins the strict-null narrowing (station requires non-null structural canonical
fields — see skein/station_store.py) and thread content-address dedup on the post-swap DDL.
"""
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skein.station_store import StationStore


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
def store():
    """A ``StationStore`` over the corpus, plus the shared name→content_hash map. The
    frozen skein_next oracle below is expressed through this ``h``, so the digests stay
    the store's own runtime values while the literals pin shape/ordering/content."""
    d = Path(tempfile.mkdtemp())
    try:
        station = StationStore(db_path=d / "station" / "skein.db")
        h = {}
        _build(station, h)
        yield station, h
        station.close()
    finally:
        shutil.rmtree(d)


_THREAD_WIRE_KEYS = ("thread_hash", "from_id", "to_id", "type", "weaver", "created_at", "content")


def _proj(thread_dict):
    """Project a thread dict to the 7 wire keys (drop the station's extra thread_id)."""
    if thread_dict is None:
        return None
    return {k: thread_dict.get(k) for k in _THREAD_WIRE_KEYS}


# ── frozen skein_next oracle ─────────────────────────────────────────────────────
# Captured from a live SkeinNextStore over the corpus above, before skein_next was
# deleted. A folio accessor returns exactly the input fields plus the content_hash.

def _folio(h, name):
    """The folio dict every folio accessor returns for ``name`` (fields + content_hash)."""
    return dict(FOLIOS[name], content_hash=h[name])


# Content-addressed thread hashes (deterministic; matched skein_next byte-for-byte).
TH_NOTE_SITE = "sha256::0d264b73583109d9dc89342e7c39c92e21ba98fba96aa6700d356a649f0dcaf1"
TH_CROSS_SITE = "sha256::420f07b22825f2702059c96d1de59ef61d3ebba0308d4e8fdc3341019d6a9446"
TH_FINDING_SITE = "sha256::84f832e6685909043264001669ce0b42ed05e2c34404fd6120f557cbb2ebcf91"
TH_ISSUE_SITE = "sha256::abe1b96cda5d98fdeeceb5dae5a4395b957f16660a2dbece4ae85e26190316b0"
TH_CROSS_SITE2 = "sha256::d05603f891654561285272d2f0701185227816edad869975d8edcc66918fe28c"
TH_STATUS_OPEN = "sha256::7bc2a55f20363fa3c736ffd85d1297c499d9e69e924af77779196637f30eafc3"
TH_STATUS_CLOSED = "sha256::fe0d77ee054203319ef0bc47a15738de614599c72a4da6134ee4f28a6082ada7"


def _thread(thash, frm, to, ttype, weaver, created_at, content):
    return {"thread_hash": thash, "from_id": frm, "to_id": to, "type": ttype,
            "weaver": weaver, "created_at": created_at, "content": content}


def _within_threads(h):
    """The five membership edges, projected, keyed by from→to for readable ordering."""
    return {
        "note_site": _thread(TH_NOTE_SITE, h["note"], h["site"], "within", None, THREAD_CREATED, None),
        "cross_site": _thread(TH_CROSS_SITE, h["cross"], h["site"], "within", None, THREAD_CREATED, None),
        "finding_site": _thread(TH_FINDING_SITE, h["finding"], h["site"], "within", None, THREAD_CREATED, None),
        "issue_site": _thread(TH_ISSUE_SITE, h["issue"], h["site"], "within", None, THREAD_CREATED, None),
        "cross_site2": _thread(TH_CROSS_SITE2, h["cross"], h["site2"], "within", None, THREAD_CREATED, None),
    }


def _status_threads(h):
    """The two status edges on issue (older 'open', newer 'closed'), projected."""
    return {
        "open": _thread(TH_STATUS_OPEN, h["issue"], h["issue"], "status", "agent-2",
                        "2026-02-02T00:00:00+00:00", "open"),
        "closed": _thread(TH_STATUS_CLOSED, h["issue"], h["issue"], "status", "agent-2",
                          "2026-02-03T00:00:00+00:00", "closed"),
    }


# ── folio accessors (dict equality) ─────────────────────────────────────────────

def test_get_folio_matches(store):
    station, h = store
    for name, ch in h.items():
        assert station.get_folio(ch) == _folio(h, name), name
    # missing → None (never {})
    assert station.get_folio("sha256::deadbeef") is None


def test_list_folios_matches(store):
    station, h = store
    order = ["site", "issue", "finding", "note", "site2", "cross"]
    assert station.list_folios() == [_folio(h, n) for n in order]
    assert station.list_folios(limit=2) == [_folio(h, n) for n in order[:2]]
    assert station.list_folios(limit=2, offset=2) == [_folio(h, n) for n in order[2:4]]


def test_recent_folios_newest_first(store):
    station, h = store
    # created_at DESCending; the finding/note tie at 2026-01-03 keeps the
    # content_hash-ASCending tiebreak list_folios uses (finding before note), so
    # this is NOT a blind reverse of the ascending list — it is the catalog's
    # newest-N, computed in SQL (ORDER BY ... LIMIT) instead of a full Python scan.
    order = ["cross", "site2", "finding", "note", "issue", "site"]
    assert station.recent_folios() == [_folio(h, n) for n in order]
    assert station.recent_folios(limit=3) == [_folio(h, n) for n in order[:3]]
    assert station.recent_folios(limit=1) == [_folio(h, "cross")]


@pytest.mark.parametrize("q, names", [
    ("login", ["finding", "issue", "note"]),
    ("login bug", ["issue"]),
    ("50%", ["issue"]),
    ("a_b", ["finding"]),
    ("LOGIN", ["finding", "issue", "note"]),
    ("nomatch", []),
    ("", []),
    ("login faster", ["note"]),
])
def test_search_folios_matches(store, q, names):
    station, h = store
    assert station.search_folios(q) == [_folio(h, n) for n in names]
    # limit=1 → the first result (or empty)
    assert station.search_folios(q, limit=1) == [_folio(h, n) for n in names[:1]]


def test_search_probe_does_not_widen_window(tmp_path):
    """>window matches: the overflow probe serves the SAME top-N as the served-limit
    window, NEVER a widened one (finding-20260710-lx37 fix #4).

    Candidate window = ``max(limit*5, 200)`` → 500 rows for limit=100. Corpus: 500 recent
    body-only matches (score 1) fill that window; 5 OLDER title matches (score 3) sit at
    recency-rank 501-505 — inside a widened (limit=101 → 505-row) window but OUTSIDE the
    served (500-row) one. Probing at limit+1 would pull those 5 higher-scored rows in and
    displace 5 recent body rows from the served top-100. ``overflow_probe`` keeps the
    window at the served limit and only returns one extra ranked row, so the served set
    stays byte-identical to the narrow-window algorithm.
    """
    station = StationStore(db_path=tmp_path / "station" / "skein.db")
    try:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with station.transaction():
            # 5 OLDER title matches (score 3): recency-rank 501-505 in a widened window.
            for j in range(5):
                station.create_folio({
                    "type": "finding", "title": f"needle title {j}", "content": f"filler {j}",
                    "created_at": base + timedelta(seconds=j), "created_by": "a",
                })
            # 500 NEWER body-only matches (score 1): they alone fill the served window.
            for i in range(500):
                station.create_folio({
                    "type": "finding", "title": f"body {i}", "content": f"needle in body {i}",
                    "created_at": base + timedelta(seconds=1000 + i), "created_by": "a",
                })

        served = station.search_folios("needle", limit=100)                      # narrow window
        probed = station.search_folios("needle", limit=100, overflow_probe=True)
        widened = station.search_folios("needle", limit=101)                     # widened window

        # The probe returns exactly one extra row (overflow signalled), and its served
        # slice is byte-identical (dict-for-dict, not just hashes) to the narrow window.
        assert len(probed) == 101
        assert probed[:100] == served
        # Every served row is a body-only match — no older title row leaked into the top.
        assert all("needle" not in r["title"].lower() for r in served)
        # The widened window WOULD serve a different top-100 (5 title rows displace 5 body
        # rows): proof this invariance is load-bearing, and the fix does NOT serve it.
        assert [r["content_hash"] for r in widened[:100]] != [r["content_hash"] for r in served]
    finally:
        station.close()


def test_search_probe_contract_in_the_small(store):
    """The overflow_probe contract in the small: at exactly the served ``limit`` there is
    no extra row (no false overflow); one match past ``limit`` yields exactly one extra
    ranked row whose head is the plain served set — the probe row is the excluded tail."""
    station, _ = store  # 3 "login" matches: finding, issue, note (ranked order)
    served_2 = station.search_folios("login", limit=2)
    probe_2 = station.search_folios("login", limit=2, overflow_probe=True)
    assert len(probe_2) == 3          # 3 matches > limit 2 → one extra row present
    assert probe_2[:2] == served_2    # served head is byte-identical to the plain limit
    served_3 = station.search_folios("login", limit=3)
    probe_3 = station.search_folios("login", limit=3, overflow_probe=True)
    assert len(probe_3) == 3          # exactly 3 matches at limit 3 → no extra row
    assert probe_3 == served_3


def test_find_by_prefix_matches(store):
    station, h = store
    # a real folio's own hash prefix, plus the framed algo prefix
    pfx = h["issue"][: len("sha256::") + 6]
    assert station.find_by_prefix(pfx) == [h["issue"]]
    order = ["finding", "issue", "site", "site2", "note", "cross"]
    assert station.find_by_prefix("sha256::") == [h[n] for n in order]
    assert station.find_by_prefix("sha256::", limit=2) == [h[n] for n in order[:2]]


def test_folios_in_site_matches(store):
    station, h = store
    assert station.folios_in_site(h["site"]) == [_folio(h, n) for n in ("issue", "finding", "note", "cross")]
    assert station.folios_in_site(h["site2"]) == [_folio(h, "cross")]
    # type filter + limit params (present but rarely driven)
    assert station.folios_in_site(h["site"], type="issue") == [_folio(h, n) for n in ("issue", "cross")]
    assert station.folios_in_site(h["site"], limit=2) == [_folio(h, n) for n in ("issue", "finding")]


def test_count_folios_matches(store):
    station, _ = store
    assert station.count_folios() == len(FOLIOS)


# ── thread accessors (7-key projection; station carries an extra thread_id) ──────

def test_get_thread_matches(store):
    station, h = store
    th = station.save_thread(from_id=h["note"], to_id=h["site"], type="within",
                             created_at=THREAD_CREATED)  # already exists → same hash
    assert _proj(station.get_thread(th)) == _within_threads(h)["note_site"]
    assert station.get_thread("sha256::missing") is None


def test_get_threads_matches(store):
    station, h = store
    def proj_list(rows):
        return [_proj(r) for r in rows]
    w = _within_threads(h)
    st = _status_threads(h)
    assert proj_list(station.get_threads(to_id=h["site"])) == [
        w["note_site"], w["cross_site"], w["finding_site"], w["issue_site"]]
    assert proj_list(station.get_threads(type="within")) == [
        w["note_site"], w["cross_site"], w["finding_site"], w["issue_site"], w["cross_site2"]]
    assert proj_list(station.get_threads(type="status")) == [st["open"], st["closed"]]
    assert proj_list(station.get_threads(from_id=h["cross"])) == [w["cross_site"], w["cross_site2"]]
    assert proj_list(station.get_threads()) == [
        w["note_site"], w["cross_site"], w["finding_site"], w["issue_site"], w["cross_site2"],
        st["open"], st["closed"]]


def test_thread_dedup_on_hash(store):
    station, h = store
    before = len(station.get_threads())
    # re-saving byte-identical wire thread → same hash, no new row
    again = station.save_thread(from_id=h["issue"], to_id=h["site"], type="within",
                                created_at=THREAD_CREATED)
    assert again in {t["thread_hash"] for t in station.get_threads()}
    assert len(station.get_threads()) == before


def test_station_thread_dict_has_thread_id(store):
    station, h = store
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

def test_create_folio_rejects_null_field():
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


def test_save_thread_rejects_null_field():
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

def test_resolve_alias_matches(store):
    station, h = store
    # aliases are populated out-of-band (station has no set_alias); insert directly.
    station.conn.execute("INSERT INTO aliases (legacy_id, content_hash) VALUES (?, ?)",
                         ("issue-20260101-aaaa", h["issue"]))
    station.conn.commit()
    assert station.resolve_alias("issue-20260101-aaaa") == h["issue"]
    assert station.resolve_alias("nope") is None


# ── read-only open ─────────────────────────────────────────────────────────────

def test_read_only_open_no_ddl_and_reads():
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


def test_read_only_open_special_char_path():
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


def test_rejects_workbench_db_unaltered():
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


def test_rejects_spoofed_station_slugs():
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


def test_logdatabase_keeps_rollback_journal():
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


def test_second_writer_with_first_open_ok():
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


# ── latest_statuses (control) ────────────────────────────────────────────────────

def test_latest_statuses_matches(store):
    station, h = store
    ids = [h["issue"], h["finding"], h["note"]]
    # issue has two status edges (open→closed); newest wins
    assert station.latest_statuses(ids) == {h["issue"]: "closed"}
    assert station.latest_statuses(ids).get(h["issue"]) == "closed"


def test_latest_statuses_empty_never_open(store):
    station, h = store
    assert station.latest_statuses([]) == {}
    # a folio with no status thread is ABSENT, never defaulted to 'open'
    got = station.latest_statuses([h["finding"]])
    assert h["finding"] not in got
    assert got == {}


# ── slugs (degenerate site case is skein_next-equivalent) ────────────────────────

def _set_site_slugs(store, h):
    store.set_slug("alpha", h["site"])
    store.set_slug("beta", h["site2"])


def test_set_resolve_list_slugs_matches(store):
    station, h = store
    _set_site_slugs(station, h)
    assert station.resolve_slug("alpha") == h["site"]
    assert station.resolve_slug("beta") == h["site2"]
    assert station.resolve_slug("absent") is None
    assert station.list_slugs() == [("alpha", h["site"]), ("beta", h["site2"])]
    # last-write-wins re-bind
    station.set_slug("alpha", h["site2"])
    assert station.resolve_slug("alpha") == h["site2"]


def test_folio_site_slug_matches(store):
    station, h = store
    _set_site_slugs(station, h)
    for name in ("issue", "finding", "note", "cross"):
        assert station.folio_site_slug(h[name]) == "alpha", name
    # cross is in both sites → alphabetically-first slug ('alpha')
    assert station.folio_site_slug(h["cross"]) == "alpha"


def test_folio_site_slugs_matches(store):
    station, h = store
    _set_site_slugs(station, h)
    assert station.folio_site_slugs() == {h[n]: "alpha" for n in ("issue", "finding", "note", "cross")}
    subset = [h["issue"], h["cross"]]
    assert station.folio_site_slugs(subset) == {h["issue"]: "alpha", h["cross"]: "alpha"}


# ── derived-head resolution (station-only — skein_next's flat slugs can't do this) ──

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
