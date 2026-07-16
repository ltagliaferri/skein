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
    new_anchor = "sha256::" + "c" * 64
    r = _run(tmp_path, "slug-override", "--slug", "specs", "--anchor", new_anchor)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        claim = st.store.get_slug_claim("specs")
        assert claim["anchor_hash"] == new_anchor
        assert claim["claimed_by_subject"] == OP  # now the operator's
