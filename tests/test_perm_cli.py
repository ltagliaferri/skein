"""Permission model — the felled-around CLI verbs: grant / rebind / slug-override
(rev 6 §3.2/§6). Screen-reader plain-text output, one item per line."""

from __future__ import annotations

from click.testing import CliRunner

from client.cli import cli
from skein.station import Station

ISS, OP = "https://accounts.google.com", "op@example.com"
BOB = "bob@example.com"
ANCHOR = "sha256::" + "a" * 64


def _run(tmp_path, *args):
    return CliRunner().invoke(
        cli, ["station", "--data-dir", str(tmp_path / ".skein-station"), *args]
    )


def _open(tmp_path):
    return Station(tmp_path / ".skein-station")


def _init_op(tmp_path):
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", OP)


# --- grant issue / revoke / list --------------------------------------------


def test_grant_issue_and_list(tmp_path):
    _init_op(tmp_path)
    r = _run(tmp_path, "grant", "issue", "--anchor", ANCHOR,
             "--issuer", ISS, "--subject", BOB, "--kind", "supersede")
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        assert st.store.has_active_grant(ANCHOR, ISS, BOB, "supersede")
    out = _run(tmp_path, "grant", "list").output
    assert f"supersede {ANCHOR} {ISS}/{BOB}" in out
    assert "|" not in out  # a11y: no tables


def test_grant_revoke(tmp_path):
    _init_op(tmp_path)
    _run(tmp_path, "grant", "issue", "--anchor", ANCHOR,
         "--issuer", ISS, "--subject", BOB, "--kind", "supersede")
    r = _run(tmp_path, "grant", "revoke", "--anchor", ANCHOR,
             "--issuer", ISS, "--subject", BOB, "--kind", "supersede")
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        assert not st.store.has_active_grant(ANCHOR, ISS, BOB, "supersede")
    # revoking again errors (nothing active)
    r2 = _run(tmp_path, "grant", "revoke", "--anchor", ANCHOR,
              "--issuer", ISS, "--subject", BOB, "--kind", "supersede")
    assert r2.exit_code != 0


def test_grant_revoke_all_by_containment(tmp_path):
    _init_op(tmp_path)
    with _open(tmp_path) as st:
        # two grants vouched by an administrator, one by the operator
        st.store.add_grant(ANCHOR, ISS, BOB, "supersede", ISS, "admin@x")
        st.store.add_grant(ANCHOR, ISS, BOB, "site_contribute", ISS, "admin@x")
        st.store.add_grant(ANCHOR, ISS, "carol@x", "supersede", ISS, OP)
    r = _run(tmp_path, "grant", "revoke-all-by", "--issuer", ISS, "--subject", "admin@x")
    assert r.exit_code == 0 and "revoked 2" in r.output
    with _open(tmp_path) as st:
        assert not st.store.has_active_grant(ANCHOR, ISS, BOB, "supersede")
        assert st.store.has_active_grant(ANCHOR, ISS, "carol@x", "supersede")  # survives


def test_grant_issue_requires_operator(tmp_path):
    r = _run(tmp_path, "grant", "issue", "--anchor", ANCHOR,
             "--issuer", ISS, "--subject", BOB, "--kind", "supersede")
    assert r.exit_code != 0 and "no operator" in r.output


def test_grant_issue_rejects_unknown_kind(tmp_path):
    _init_op(tmp_path)
    r = _run(tmp_path, "grant", "issue", "--anchor", ANCHOR,
             "--issuer", ISS, "--subject", BOB, "--kind", "bogus")
    assert r.exit_code != 0  # click.Choice rejects it


# --- rebind -----------------------------------------------------------------


def test_rebind_changes_tier(tmp_path):
    _init_op(tmp_path)
    _run(tmp_path, "account", "add", "--issuer", ISS, "--subject", BOB, "--role", "originator")
    r = _run(tmp_path, "account", "rebind", "--issuer", ISS, "--subject", BOB, "--role", "steward")
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        assert st.store.get_binding(ISS, BOB).role == "steward"


def test_rebind_refuses_operator(tmp_path):
    _init_op(tmp_path)
    # rebinding the operator is refused (goes through rotate-operator)
    r = _run(tmp_path, "account", "rebind", "--issuer", ISS, "--subject", OP, "--role", "steward")
    assert r.exit_code != 0 and "rotate-operator" in r.output


# --- slug-override ----------------------------------------------------------


def test_slug_override_repoints(tmp_path):
    _init_op(tmp_path)
    with _open(tmp_path) as st:
        st.store.claim_slug("specs", "sha256::" + "b" * 64, ISS, BOB)  # bob owns it
        # the new anchor must name a HELD folio (the override guard refuses otherwise)
        new_anchor = st.store.create_folio({
            "type": "site", "title": "op site", "content": "b",
            "created_at": "2026-07-16T00:00:00+00:00", "created_by": "op",
        })
    r = _run(tmp_path, "slug-override", "--slug", "specs", "--anchor", new_anchor)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        claim = st.store.get_slug_claim("specs")
        assert claim["anchor_hash"] == new_anchor
        assert claim["claimed_by_subject"] == OP  # now the operator's


def test_slug_override_normalizes_to_genesis(tmp_path):
    """audit-cap: slug-override must anchor at the lineage GENESIS, not a head — a
    head-anchored slug orphans members filed under the genesis."""
    _init_op(tmp_path)
    with _open(tmp_path) as st:
        g = st.store.create_folio({
            "type": "site", "title": "site v1", "content": "b",
            "created_at": "2026-07-16T00:00:00+00:00", "created_by": "op",
        })
        v2 = st.store.create_folio({
            "type": "site", "title": "site v2", "content": "b",
            "created_at": "2026-07-16T01:00:00+00:00", "created_by": "op",
        })
        st.store.save_thread(from_id=v2, to_id=g, type="supersedes",
                             created_at="2026-07-16T02:00:00+00:00")
    # override pointing at the HEAD v2 must normalize down to the genesis g
    r = _run(tmp_path, "slug-override", "--slug", "specs", "--anchor", v2)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        assert st.store.get_slug_claim("specs")["anchor_hash"] == g   # genesis, not v2
        assert st.store.resolve_slug("specs") == v2                   # head derived


def test_slug_override_refuses_unheld_anchor(tmp_path):
    """fell-r3: an unheld anchor is refused — the slug would resolve to nothing."""
    _init_op(tmp_path)
    r = _run(tmp_path, "slug-override", "--slug", "specs", "--anchor", "sha256::" + "c" * 64)
    assert r.exit_code != 0 and "not a held folio" in r.output


def test_slug_override_on_a_defective_lineage_names_the_recovery(tmp_path):
    """A structurally-broken anchor fails closed AND names the atomic recovery command
    (the migration module), not the unsafe `_repair_supersedes` helper. This is the
    operator's dead end: without the command they know the name is unusable but not how
    to fix it."""
    _init_op(tmp_path)
    with _open(tmp_path) as st:
        anchor = st.store.create_folio({
            "type": "site", "title": "defective site", "content": "b",
            "created_at": "2026-07-16T00:00:00+00:00", "created_by": "op",
        })
        # a self-edge: the genesis resolver fails closed on it forever
        st.store.save_thread(from_id=anchor, to_id=anchor, type="supersedes",
                             created_at="2026-07-16T01:00:00+00:00")

    r = _run(tmp_path, "slug-override", "--slug", "specs", "--anchor", anchor)

    assert r.exit_code != 0, r.output
    assert "no single lineage genesis" in r.output, r.output
    assert "python -m skein.migrations.perm_model_rev6" in r.output, r.output
    assert "_repair_supersedes" not in r.output, r.output
    with _open(tmp_path) as st:
        assert st.store.get_slug_claim("specs") is None   # still fails closed


def test_supersedes_dedupes_so_an_idempotent_republish_does_not_brick(tmp_path):
    """A re-published supersedes edge the station ALREADY holds must NOT read as a merge.

    The realistic idempotent re-publish is a batch re-sending an edge already stored: it
    authorizes, enters the pending/staged set, and `_supersedes_parents` then sees the
    SAME parent from two sources — the stored row and the pending copy. Without the
    distinct-parents dedupe those are `[g, g]`, the >1-parent check fires, and the whole
    lineage bricks (every op on it refused until an offline migration). Pins that the
    stored+pending duplicate collapses to one parent and the lineage still resolves.

    (Note: two STORED copies cannot arise — `save_thread` is INSERT-OR-IGNORE — so the
    dedupe only bites on the stored+pending path, which is what this drives.)"""
    from skein.thread_authz import _supersedes_parents, lineage_genesis_for

    with _open(tmp_path) as st:
        g = st.store.create_folio({
            "type": "site", "title": "site v1", "content": "b",
            "created_at": "2026-07-16T00:00:00+00:00", "created_by": "op",
        })
        v2 = st.store.create_folio({
            "type": "site", "title": "site v2", "content": "b",
            "created_at": "2026-07-16T01:00:00+00:00", "created_by": "op",
        })
        st.store.save_thread(from_id=v2, to_id=g, type="supersedes",
                             created_at="2026-07-16T02:00:00+00:00")
        # the batch re-sends the edge it already holds: same parent, now also pending
        pending = [(v2, g)]

        assert _supersedes_parents(st.store, v2, pending) == [g]        # not [g, g]
        assert lineage_genesis_for(st.store, v2, pending).hash == g     # resolves
