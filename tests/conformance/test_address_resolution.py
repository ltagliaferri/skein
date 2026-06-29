"""Commit D conformance: the Phase 1 resolver wired over versions/refs.

Round-trip resolution for every address form alongside the others:
  - sha256::<64-hex> / hash::sha256::<64-hex>   -> the exact version
  - alias::<station>::sha256::<short>           -> lengthen via the versions prefix scan
  - <slug> / ref::<slug>                        -> the local lineage head
  - project:id (single colon)                   -> UNCHANGED, through address_legacy
Plus the by-hash fetch flags (is_head / lineage_head) and the refuse-don't-guess
behaviour on an unknown slug / ambiguous short hash.
"""

import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skein import address as A
from skein.models import Folio, Site
from skein.storage import JSONStore


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d)


@pytest.fixture
def store(tmp_dir):
    s = JSONStore(tmp_dir)
    s.save_site(Site(site_id="sa", created_at=datetime.now(timezone.utc),
                     created_by="t", purpose="p"))
    return s


def _head_hash(store, slug):
    c = sqlite3.connect(store.base_dir / "skein.db")
    try:
        return c.execute("SELECT head_hash FROM refs WHERE slug=?", (slug,)).fetchone()[0]
    finally:
        c.close()


def _two_version_lineage(store, slug="finding-20260629-rslv"):
    f = Folio(folio_id=slug, type="finding", site_id="sa",
              created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), created_by="a",
              title="T", content="v1 content", status="open", metadata={})
    store.save_folio(f)
    v1 = _head_hash(store, slug)
    g = store.get_folio(slug)
    g.content = "v2 content"
    store.save_folio(g, editor="e")
    v2 = _head_hash(store, slug)
    return slug, v1, v2


def test_resolve_slug_and_ref_to_head(store):
    slug, v1, v2 = _two_version_lineage(store)
    st = store.station_index()
    v2hex = v2.split("::")[1]
    assert A.resolve(A.parse(slug), st) == v2hex
    assert A.resolve(A.parse(f"ref::{slug}"), st) == v2hex


def test_resolve_full_and_explicit_hash(store):
    slug, v1, v2 = _two_version_lineage(store)
    st = store.station_index()
    v1hex = v1.split("::")[1]
    # A full hash returns its own digest WITHOUT querying (resolves a superseded one).
    assert A.resolve(A.parse(f"sha256::{v1hex}"), st) == v1hex
    assert A.resolve(A.parse(f"hash::sha256::{v1hex}"), st) == v1hex


def test_resolve_station_scoped_short_hash(store):
    slug, v1, v2 = _two_version_lineage(store)
    st = store.station_index()
    v1hex = v1.split("::")[1]
    # Short hashes are station-scoped; lengthen against ALL versions (incl. v1).
    assert A.resolve(A.parse(f"alias::mystation::sha256::{v1hex[:12]}"), st) == v1hex
    # A BARE short hash is invalid (no station to cascade against).
    assert A.validate(f"sha256::{v1hex[:12]}") is False


def test_by_hash_fetch_flags(store):
    slug, v1, v2 = _two_version_lineage(store)
    # Superseded version: immutable content, is_head False, lineage_head = current head.
    bh1 = store.get_version_by_hash(v1)
    assert bh1.content == "v1 content"
    assert bh1.content_hash == v1
    assert bh1.is_head is False
    assert bh1.lineage_head == v2
    # VersionView carries NO mutable control (no status/assigned_to fields).
    assert not hasattr(bh1, "status")
    # Head version: is_head True, lineage_head itself.
    bh2 = store.get_version_by_hash(v2)
    assert bh2.is_head is True
    assert bh2.lineage_head == v2
    assert store.get_version_by_hash("sha256::" + "0" * 64) is None


def test_unknown_slug_and_ambiguous_refuse(store):
    slug, v1, v2 = _two_version_lineage(store)
    st = store.station_index()
    with pytest.raises(A.AddressError):
        A.resolve(A.parse("ref::nope-00000000-zzzz"), st)
    # An ambiguous short prefix refuses rather than guessing. '' is the common
    # prefix of v1 and v2, so a short prefix they share raises AmbiguousShortHash.
    shared = ""
    a, b = v1.split("::")[1], v2.split("::")[1]
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        shared += ca
    if len(shared) >= 8:  # only assert if they actually share an 8+ prefix
        with pytest.raises(A.AmbiguousShortHash):
            A.resolve(A.parse(f"alias::s::sha256::{shared}"), st)


def test_route_helper_dispatches_forms(store):
    from skein.routes import _resolve_rev3_read
    slug, v1, v2 = _two_version_lineage(store)
    # ref form -> head folio (with control).
    folio = _resolve_rev3_read(f"ref::{slug}", store)
    assert folio.folio_id == slug and folio.content == "v2 content"
    # bare slug WITHOUT '::' is NOT a rev3 dispatch (legacy handles it); but the
    # ref:: explicit form is. A hash form -> by-hash version.
    v1hex = v1.split("::")[1]
    byhash = _resolve_rev3_read(f"sha256::{v1hex}", store)
    assert byhash.content == "v1 content" and byhash.is_head is False


def test_verifier_fragment_enforced(store):
    from fastapi import HTTPException
    from skein.routes import _resolve_rev3_read
    slug, v1, v2 = _two_version_lineage(store)
    v1hex, v2hex = v1.split("::")[1], v2.split("::")[1]

    # A matching fragment passes (the address resolves to exactly the asserted hash).
    ok = _resolve_rev3_read(f"sha256::{v1hex}#sha256::{v1hex}", store)
    assert ok.content == "v1 content"
    # A mismatched fragment REJECTS (resolves to v1 but asserts v2).
    with pytest.raises(HTTPException) as ei:
        _resolve_rev3_read(f"sha256::{v1hex}#sha256::{v2hex}", store)
    assert ei.value.status_code == 409
    # ref:: with a fragment matching the head passes; mismatched rejects.
    okref = _resolve_rev3_read(f"ref::{slug}#sha256::{v2hex}", store)
    assert okref.folio_id == slug
    with pytest.raises(HTTPException):
        _resolve_rev3_read(f"ref::{slug}#sha256::{v1hex}", store)


def test_fragment_on_legacy_bare_slug_preserves_cascade(store):
    # A verifier fragment on a BARE slug (no `::` in the body) must NOT force the
    # rev3 path / drop the cascade: the body decides the scheme, the fragment is
    # enforced against the resolved head. (resolve_folio_read is the dispatch.)
    from fastapi import HTTPException
    from skein.routes import resolve_folio_read
    slug, v1, v2 = _two_version_lineage(store)
    v1hex, v2hex = v1.split("::")[1], v2.split("::")[1]
    # Matching fragment on the bare slug -> the head folio (local cascade path).
    f = resolve_folio_read(f"{slug}#sha256::{v2hex}", None, store)
    assert f.folio_id == slug and f.content == "v2 content"
    # Mismatched fragment on the bare slug -> 409, not a wrong folio.
    with pytest.raises(HTTPException) as ei:
        resolve_folio_read(f"{slug}#sha256::{v1hex}", None, store)
    assert ei.value.status_code == 409


def test_legacy_single_colon_unregressed():
    # project:id (single colon, no '::') still parses through address_legacy.
    from skein.address_legacy import parse as legacy_parse
    p = legacy_parse("speakbot:brief-20251226-xyz")
    assert p.is_qualified and p.project == "speakbot" and p.folio_id == "brief-20251226-xyz"
    bare = legacy_parse("brief-20251226-xyz")
    assert not bare.is_qualified and bare.folio_id == "brief-20251226-xyz"
