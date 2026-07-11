"""Station-ops CLI: invite (mint/list/revoke), whoami, redeem-invite surface.

Re-homed from skein_next/tests/test_cli_invite.py (station re-home Stage 6) onto
the ``skein station`` group. The invite verbs flatten one level: ``interskein
account invite mint`` becomes ``skein station invite mint`` (account, invite,
redeem are peers per the design's station-ops surface). The redeem-invite client
and the live ceremony are exercised by the e2e/route tests; here we cover the
operator-side invite lifecycle (no Sigstore needed) and the whoami identity
print (sigstore session mocked).

Also pins the Stage-6 carry from finding-20260709-p4n5 #2: a NAIVE expires_at
reaching mint_invite must be treated as UTC, never reinterpreted through the
system-local timezone (the ``_iso_micros`` footgun).
"""

from __future__ import annotations

import time

from click.testing import CliRunner

from client.cli import cli
from skein.identity import hash_token
from skein.station import Station

OP_ISS, OP_SUB = "https://accounts.google.com", "op@example.com"


def _run(tmp_path, *args):
    return CliRunner().invoke(
        cli, ["station", "--data-dir", str(tmp_path / ".skein-station"), *args]
    )


def _open(tmp_path):
    return Station(tmp_path / ".skein-station")


def _init_op(tmp_path):
    return _run(
        tmp_path, "account", "init-operator", "--issuer", OP_ISS, "--subject", OP_SUB
    )


def test_mint_prints_token_and_records_hash(tmp_path):
    _init_op(tmp_path)
    r = _run(
        tmp_path, "invite", "mint", "--note", "Alice", "--origin", "https://interskein.com"
    )
    assert r.exit_code == 0, r.output
    assert "redeem-invite" in r.output and "interskein.com" in r.output
    # the blurb instructs the NEW verb surface, not the retired interskein CLI
    assert "skein station redeem-invite" in r.output
    assert "interskein redeem-invite" not in r.output
    # the printed token redeems to the stored hash
    with _open(tmp_path) as st:
        rows = st.store.list_invites()
        assert len(rows) == 1
        assert rows[0]["note"] == "Alice" and rows[0]["vouched_by_subject"] == OP_SUB
        assert rows[0]["used_at"] is None


def test_mint_json_token_hashes_to_stored_row(tmp_path):
    import json

    _init_op(tmp_path)
    r = _run(tmp_path, "invite", "mint", "--json")
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert hash_token(payload["token"]) == payload["token_hash"]
    with _open(tmp_path) as st:
        assert st.store.get_invite_by_token_hash(payload["token_hash"]) is not None


def test_mint_bad_duration_errors(tmp_path):
    _init_op(tmp_path)
    r = _run(tmp_path, "invite", "mint", "--expires", "soon")
    assert r.exit_code != 0 and "30m" in r.output


def test_mint_without_operator_errors(tmp_path):
    r = _run(tmp_path, "invite", "mint")
    assert r.exit_code != 0 and "operator" in r.output


def test_mint_origin_defaults_from_station_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKEIN_STATION_ORIGIN", "https://interskein.com")
    _init_op(tmp_path)
    r = _run(tmp_path, "invite", "mint")
    assert r.exit_code == 0
    assert "https://interskein.com" in r.output
    assert "placeholder" not in r.output


def test_list_shows_outstanding_then_revoked(tmp_path):
    import json

    _init_op(tmp_path)
    r = _run(tmp_path, "invite", "mint", "--json", "--note", "Bob")
    token = json.loads(r.output)["token"]
    out = _run(tmp_path, "invite", "list").output
    assert "outstanding author" in out and "Bob" in out
    # revoke by token, then it drops from the default list, shows under --all
    rr = _run(tmp_path, "invite", "revoke", token)
    assert rr.exit_code == 0 and "revoked invite" in rr.output
    assert "outstanding" not in _run(tmp_path, "invite", "list").output
    assert "revoked author" in _run(tmp_path, "invite", "list", "--all").output


def test_revoke_by_hash_prefix(tmp_path):
    import json

    _init_op(tmp_path)
    th = json.loads(_run(tmp_path, "invite", "mint", "--json").output)["token_hash"]
    r = _run(tmp_path, "invite", "revoke", "--hash", th[:12])
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        assert st.store.get_invite_by_token_hash(th)["revoked_at"] is not None


def test_revoke_unknown_errors(tmp_path):
    _init_op(tmp_path)
    r = _run(tmp_path, "invite", "revoke", "no-such-token")
    assert r.exit_code != 0 and "no active invite" in r.output


def test_revoke_empty_hash_prefix_refused(tmp_path):
    """`--hash ""` startswith-matches every row; with exactly one outstanding
    invite it would resolve 'unambiguously' and silently revoke it — a failed
    shell interpolation must be an error, not a revocation (deep_code_audit r4)."""
    import json

    _init_op(tmp_path)
    th = json.loads(_run(tmp_path, "invite", "mint", "--json").output)["token_hash"]
    r = _run(tmp_path, "invite", "revoke", "--hash", "")
    assert r.exit_code != 0 and "empty invite hash prefix" in r.output
    with _open(tmp_path) as st:
        assert st.store.get_invite_by_token_hash(th)["revoked_at"] is None  # untouched


def test_whoami_prints_identity(tmp_path, monkeypatch):
    from skein import sign as sign_mod

    class _Sess:
        issuer = "https://accounts.google.com"
        subject = "carol@example.com"

    monkeypatch.setattr(sign_mod, "acquire_oidc_session", lambda **k: _Sess())
    r = CliRunner().invoke(cli, ["station", "whoami"])
    assert r.exit_code == 0
    assert "issuer https://accounts.google.com" in r.output
    assert "subject carol@example.com" in r.output


def test_redeem_invite_requires_login(tmp_path):
    r = CliRunner().invoke(cli, ["station", "redeem-invite", "tok", "--to", "https://x"])
    assert r.exit_code != 0 and "--login" in r.output


# --- the p4n5 #2 carry: naive expires_at must mean UTC ------------------------


def test_mint_invite_naive_expires_at_is_utc_not_local(tmp_path, monkeypatch):
    """A naive datetime handed to mint_invite is stored AS UTC. Without the
    guard, ``_iso_micros`` reinterprets it as system-LOCAL time before
    converting, silently shifting the expiry by the host's UTC offset. Pin the
    process to a non-UTC zone so the unguarded path FIRES on any box."""
    from datetime import datetime, timezone

    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        _init_op(tmp_path)
        naive = datetime(2026, 7, 1, 12, 0, 0)  # meant as UTC by the caller
        with _open(tmp_path) as st:
            op = st.store.get_operator()
            st.store.mint_invite(
                hash_token("tok-naive"),
                "author",
                naive,
                vouched_by_issuer=op.issuer,
                vouched_by_subject=op.subject,
            )
            row = st.store.get_invite_by_token_hash(hash_token("tok-naive"))
        stored = datetime.fromisoformat(row["expires_at"])
        # the naive wall-clock survives verbatim as UTC — NOT shifted by -04:00
        assert stored == datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
