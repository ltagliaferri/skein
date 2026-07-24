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
        tmp_path,
        "invite",
        "mint",
        "--note",
        "Alice",
        "--origin",
        "https://ingress.interskein.com",
        "--onboarding-origin",
        "https://interskein.com",
    )
    assert r.exit_code == 0, r.output
    assert "redeem-invite" in r.output and "interskein.com" in r.output
    # the blurb instructs the NEW verb surface, not the retired interskein CLI
    assert "skein station redeem-invite" in r.output
    assert "interskein redeem-invite" not in r.output
    assert "https://interskein.com/onboarding" in r.output
    assert "--to https://ingress.interskein.com" in r.output
    assert "https://ingress.interskein.com/onboarding" not in r.output
    assert "Bootstrap freshness SHA256" in r.output
    # the printed token redeems to the stored hash
    with _open(tmp_path) as st:
        rows = st.store.list_invites()
        assert len(rows) == 1
        assert rows[0]["note"] == "Alice" and rows[0]["vouched_by_subject"] == OP_SUB
        assert rows[0]["used_at"] is None


def test_mint_json_token_hash_matches_row(tmp_path):
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


def test_mint_origin_defaults_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKEIN_STATION_ORIGIN", "https://ingress.interskein.com")
    monkeypatch.setenv("SKEIN_STATION_BASE_URL", "https://interskein.com")
    _init_op(tmp_path)
    r = _run(tmp_path, "invite", "mint")
    assert r.exit_code == 0
    assert "https://interskein.com" in r.output
    assert "placeholder" not in r.output


def test_mint_rejects_malformed_origins(tmp_path):
    _init_op(tmp_path)
    r = _run(tmp_path, "invite", "mint", "--origin", "not-a-url")
    assert r.exit_code != 0 and "invalid publish origin" in r.output
    r = _run(
        tmp_path,
        "invite",
        "mint",
        "--onboarding-origin",
        "https://user:pass@example.com/?q=bad",
    )
    assert r.exit_code != 0 and "invalid onboarding origin" in r.output


def test_minted_onboarding_url_resolves_on_read_surface(tmp_path, monkeypatch):
    import re

    from fastapi.testclient import TestClient

    from skein.web.app import create_app

    _init_op(tmp_path)
    data_dir = tmp_path / ".skein-station"
    bootstrap = data_dir / "bootstrap"
    bootstrap.mkdir()
    for raw in ("sigstore-pinned.txt", "interskein-pinned.txt", "interskein-primer.txt"):
        (bootstrap / raw).write_text(f"pinned {raw}\n")
        (bootstrap / f"{raw}.sigstore.json").write_text("{}\n")

    r = _run(
        tmp_path,
        "invite",
        "mint",
        "--origin",
        "https://ingress.example.test",
        "--onboarding-origin",
        "https://read.example.test",
    )
    assert r.exit_code == 0, r.output
    onboarding_url = re.search(r"https://read\.example\.test/onboarding", r.output).group(0)
    assert "https://ingress.example.test/onboarding" not in r.output
    for raw in ("sigstore-pinned.txt", "interskein-pinned.txt", "interskein-primer.txt"):
        from hashlib import sha256

        assert sha256((bootstrap / raw).read_bytes()).hexdigest() in r.output

    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SKEIN_STATION_NAME", "test station")
    page = TestClient(create_app()).get(onboarding_url.removeprefix("https://read.example.test"))
    assert page.status_code == 200
    assert "Install the pinned verifier" in page.text


def test_list_shows_outstanding_then_revoked(tmp_path):
    import json

    _init_op(tmp_path)
    r = _run(tmp_path, "invite", "mint", "--json", "--note", "Bob")
    token = json.loads(r.output)["token"]
    out = _run(tmp_path, "invite", "list").output
    assert "outstanding originator" in out and "Bob" in out
    # revoke by token, then it drops from the default list, shows under --all
    rr = _run(tmp_path, "invite", "revoke", token)
    assert rr.exit_code == 0 and "revoked invite" in rr.output
    assert "outstanding" not in _run(tmp_path, "invite", "list").output
    assert "revoked originator" in _run(tmp_path, "invite", "list", "--all").output


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


def test_minted_tokens_never_start_with_a_dash(monkeypatch):
    """token_urlsafe's alphabet includes '-', and a leading one reads as an
    option on the positional revoke/redeem verbs (issue-20260723-rzer). The
    mint helper re-rolls; forced dash-first draws prove the loop."""
    from skein import station_cli

    draws = iter(["-dash-first", "-dash-again", "cleanTOKEN123"])
    monkeypatch.setattr(station_cli.secrets, "token_urlsafe", lambda n: next(draws))
    assert station_cli._mint_token() == "cleanTOKEN123"


def test_mint_uses_the_guarded_helper(tmp_path, monkeypatch):
    """The command path must mint through _mint_token, not bare token_urlsafe."""
    import json

    from skein import station_cli

    _init_op(tmp_path)
    monkeypatch.setattr(station_cli, "_mint_token", lambda: "sentinel-token-xyz")
    out = json.loads(_run(tmp_path, "invite", "mint", "--json").output)
    assert out["token"] == "sentinel-token-xyz"
    assert out["token_hash"] == hash_token("sentinel-token-xyz")


def test_a_legacy_dash_token_is_revocable_after_the_separator(tmp_path):
    """Invites minted before the guard can start with '-'. Planted directly in
    the store (mint can no longer produce one), such a token must remain
    revocable via `revoke -- -TOKEN`."""
    from datetime import datetime, timedelta, timezone

    _init_op(tmp_path)
    legacy = "-LegacyDashToken123"
    with _open(tmp_path) as st:
        op = st.store.get_operator()
        st.store.mint_invite(
            hash_token(legacy),
            "originator",
            datetime.now(timezone.utc) + timedelta(days=1),
            vouched_by_issuer=op.issuer,
            vouched_by_subject=op.subject,
            note=None,
        )
    bare = _run(tmp_path, "invite", "revoke", legacy)
    assert bare.exit_code != 0 and "No such option" in bare.output
    r = _run(tmp_path, "invite", "revoke", "--", legacy)
    assert r.exit_code == 0, r.output
    with _open(tmp_path) as st:
        assert st.store.get_invite_by_token_hash(hash_token(legacy))["revoked_at"] is not None


def test_redeem_invite_accepts_a_dash_token_after_the_separator(tmp_path):
    """Parse-level proof for the redeem verb: after `--` the dash token binds
    to the TOKEN argument, so the failure is the missing --to option, not
    option-parsing on the token itself."""
    r = CliRunner().invoke(cli, ["station", "redeem-invite", "--", "-dashTOKEN"])
    assert "No such option" not in r.output
    assert "Missing option '--to'" in r.output


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


def test_mint_naive_expires_at_stored_utc(tmp_path, monkeypatch):
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
                "originator",
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
