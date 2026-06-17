"""Invite-redeem ceremony — the hard invariants (brief-20260615-ofv1).

Layers, mirroring the rest of the suite:
- verify_wire_redeem totality + the token-binding (INV-1), driven by the fake
  binding-verifier the manifest suite uses (no live Sigstore).
- the store's exactly-once burn CAS + revoked-binding guard + flood counter
  (INV-2/3/5).
- the redeem orchestration state machine (cheap-before-crypto, idempotency INV-6).
- the /invite/redeem route hardening parity (INV-5).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from skein import signing
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus, _test_factory

from skein_next import profile, redeem as redeem_mod
from skein_next import sign as sign_mod
from skein_next.identity import hash_token
from skein_next.ingress import create_app, ENV_DATA_DIR, ENV_ORIGIN, REDEEM_MAX_BYTES
from skein_next.station import Station

ORIGIN = "https://interskein.com"
ISSUER = "https://accounts.google.com"
SUBJECT = "alice@example.com"
OP = ("https://accounts.google.com", "operator@example.com")


# --- fakes ------------------------------------------------------------------


def _redeem_signer(issuer=ISSUER, subject=SUBJECT, canon_profile=profile.CANON_PROFILE_REDEEM_V1):
    """A SignedResult-returning redeem signer over the redeem profile (SG2 shape)."""

    def _sign(canonical_bytes):
        preimage = profile.profiled_preimage(canon_profile, canonical_bytes)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=["x"],
            canonical_bytes=preimage,
            canon_version=canon_profile,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)

    return _sign


def _binding_verifier(issuer=ISSUER, subject=SUBJECT):
    """Emulates verify_multi: SIGNATURE_MISMATCH if the reconstructed preimage
    diverges from the bundle's stored canonical_bytes; else VERIFIED with identity."""

    def _v(canonical_bytes, bundle):
        if bundle.canonical_bytes != canonical_bytes:
            return MultiVerifyResult(
                results=[VerifyResult(status=VerifyStatus.SIGNATURE_MISMATCH)],
                overall=VerifyStatus.SIGNATURE_MISMATCH,
            )
        return MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=issuer, subject=subject)],
            overall=VerifyStatus.VERIFIED,
        )

    return _v


def _mint(station, token="tok-" + "a" * 40, role="author", expires_in_days=7, note="Alice"):
    th = hash_token(token)
    station.store.mint_invite(
        th, role, datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        vouched_by_issuer=OP[0], vouched_by_subject=OP[1], note=note,
    )
    return token, th


def _proof(token, origin=ORIGIN, issuer=ISSUER, subject=SUBJECT, **kw):
    proof, _, _ = sign_mod.sign_redeem_proof(token, origin, _redeem_signer(issuer, subject), **kw)
    return proof


@pytest.fixture
def station(tmp_path):
    s = Station(tmp_path / "instance" / ".skein-next")
    yield s
    s.close()


# --- INV-1: verify_wire_redeem totality + token-binding ---------------------


def test_verify_redeem_happy_binds_discovered_identity():
    token = "tok-xyz"
    p = _proof(token)
    ok, reason, ident = sign_mod.verify_wire_redeem(
        p, hash_token(token), ORIGIN, sign_mod.REDEEM_ROUTE, _binding_verifier()
    )
    assert ok and reason == "verified"
    assert ident == {"issuer": ISSUER, "subject": SUBJECT}


@pytest.mark.parametrize("bad", [None, 5, "str", [], {}, {"nonce": "n"}, {"nonce": "n", "issued_at": "t"}])
def test_verify_redeem_malformed_proof_total(bad):
    ok, reason, ident = sign_mod.verify_wire_redeem(
        bad, "h", ORIGIN, sign_mod.REDEEM_ROUTE, _binding_verifier()
    )
    assert ok is False and ident is None and reason == "proof malformed"


def test_verify_redeem_oversized_nonce_is_malformed():
    token = "tok-xyz"
    p = _proof(token, nonce="n" * (sign_mod.MAX_REDEEM_NONCE_LEN + 1))
    ok, reason, _ = sign_mod.verify_wire_redeem(
        p, hash_token(token), ORIGIN, sign_mod.REDEEM_ROUTE, _binding_verifier()
    )
    assert ok is False and reason == "proof malformed"


def test_verify_redeem_harvested_folio_bundle_is_wrong_kind():
    # A bundle signed under the FOLIO profile (a harvested public manifest/folio
    # bundle) is rejected 'wrong kind' BEFORE any crypto — the cross-path pin.
    token = "tok-xyz"
    # build a proof carrying a folio-profile bundle (the harvested-bundle case)
    signer = _redeem_signer(canon_profile=profile.CANON_PROFILE_V1)
    proof, _, _ = sign_mod.sign_redeem_proof(token, ORIGIN, signer)
    ok, reason, _ = sign_mod.verify_wire_redeem(
        proof, hash_token(token), ORIGIN, sign_mod.REDEEM_ROUTE, _binding_verifier()
    )
    assert ok is False and reason == "wrong kind"


def test_verify_redeem_wrong_token_is_signature_mismatch():
    # A proof minted for token A presented against token B's hash: the reconstructed
    # challenge bytes diverge from the bundle's signed bytes -> SIGNATURE_MISMATCH.
    p = _proof("token-A")
    ok, reason, ident = sign_mod.verify_wire_redeem(
        p, hash_token("token-B"), ORIGIN, sign_mod.REDEEM_ROUTE, _binding_verifier()
    )
    assert ok is False and ident is None and reason == "SIGNATURE_MISMATCH"


def test_verify_redeem_wrong_origin_is_signature_mismatch():
    token = "tok-xyz"
    p = _proof(token, origin="https://evil.example.com")
    ok, reason, _ = sign_mod.verify_wire_redeem(
        p, hash_token(token), ORIGIN, sign_mod.REDEEM_ROUTE, _binding_verifier()
    )
    assert ok is False and reason == "SIGNATURE_MISMATCH"


# --- INV-2/3: store CAS burn + revoked-binding guard ------------------------


def test_cas_burns_exactly_once(station):
    token, th = _mint(station)
    assert station.store.redeem_invite_cas(th, ISSUER, SUBJECT) == "redeemed"
    # A second CAS (even same identity) loses the race: used_at is set, WHERE fails.
    assert station.store.redeem_invite_cas(th, ISSUER, SUBJECT) == "race_lost"
    row = station.store.get_invite_by_token_hash(th)
    assert row["used_at"] is not None
    assert row["bound_issuer"] == ISSUER and row["bound_subject"] == SUBJECT
    # exactly one active binding produced
    assert station.store.get_binding(ISSUER, SUBJECT).revoked_at is None


def test_cas_refuses_revoked_identity_and_does_not_burn(station):
    token, th = _mint(station)
    # operator binds then revokes this identity
    station.store.add_binding(ISSUER, SUBJECT, role="author")
    station.store.revoke_binding(ISSUER, SUBJECT)
    assert station.store.redeem_invite_cas(th, ISSUER, SUBJECT) == "revoked_identity"
    # token NOT burned, binding stays revoked (never reactivated, INV-3)
    assert station.store.get_invite_by_token_hash(th)["used_at"] is None
    assert station.store.get_binding(ISSUER, SUBJECT).revoked_at is not None


def test_cas_expired_token_not_burned(station):
    token, th = _mint(station, expires_in_days=-1)  # already expired
    assert station.store.redeem_invite_cas(th, ISSUER, SUBJECT) == "race_lost"
    assert station.store.get_invite_by_token_hash(th)["used_at"] is None


def test_revoke_invite_then_cas_race_lost(station):
    token, th = _mint(station)
    assert station.store.revoke_invite(th) is True
    assert station.store.revoke_invite(th) is False  # idempotent: already revoked
    assert station.store.redeem_invite_cas(th, ISSUER, SUBJECT) == "race_lost"


def test_concurrent_burn_binds_exactly_once(tmp_path):
    # INV-2 under a REAL race: many connections call the CAS on one token at once;
    # BEGIN IMMEDIATE + busy_timeout serialize them, so EXACTLY ONE burns and binds.
    import concurrent.futures as cf

    inst = tmp_path / "inst" / ".skein-next"
    s = Station(inst)
    token, th = _mint(s)
    s.close()

    def attempt(_i):
        st = Station(inst, check_same_thread=False)
        try:
            return st.store.redeem_invite_cas(th, ISSUER, SUBJECT)
        finally:
            st.close()

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        outcomes = list(ex.map(attempt, range(16)))

    assert outcomes.count("redeemed") == 1
    assert all(o in ("redeemed", "race_lost") for o in outcomes)
    check = Station(inst, check_same_thread=False)
    try:
        row = check.store.get_invite_by_token_hash(th)
        assert row["used_at"] is not None and row["bound_subject"] == SUBJECT
        # exactly one binding, active
        b = check.store.get_binding(ISSUER, SUBJECT)
        assert b is not None and b.revoked_at is None
        redeemed_events = [e for e in check.store.get_invite_events(th) if e["event"] == "redeemed"]
        assert len(redeemed_events) == 1
    finally:
        check.close()


# --- redeem orchestration (cheap-before-crypto, idempotency) ----------------


def _crypto_calls(monkeypatch):
    """Wrap verify_wire_redeem to count how often crypto verification is reached."""
    calls = {"n": 0}
    real = sign_mod.verify_wire_redeem

    def _counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(sign_mod, "verify_wire_redeem", _counted)
    return calls


def _do(station, token, **kw):
    return redeem_mod.redeem(station, token, _proof(token), ORIGIN, verifier=_binding_verifier(), **kw)


def test_redeem_happy_binds_author(station):
    token, th = _mint(station)
    r = _do(station, token)
    assert r.ok and r.status == redeem_mod.RedeemStatus.OK_REDEEMED
    assert (r.issuer, r.subject) == (ISSUER, SUBJECT)
    b = station.store.get_binding(ISSUER, SUBJECT)
    assert b is not None and b.role == "author" and b.revoked_at is None


def test_redeem_unknown_token_cheap(station, monkeypatch):
    calls = _crypto_calls(monkeypatch)
    r = redeem_mod.redeem(station, "no-such-token", {"x": 1}, ORIGIN, verifier=_binding_verifier())
    assert not r.ok and r.status == redeem_mod.RedeemStatus.UNKNOWN
    assert calls["n"] == 0  # rejected BEFORE crypto


def test_redeem_revoked_invite_cheap(station, monkeypatch):
    token, th = _mint(station)
    station.store.revoke_invite(th)
    calls = _crypto_calls(monkeypatch)
    r = _do(station, token)
    assert not r.ok and r.status == redeem_mod.RedeemStatus.REVOKED_INVITE
    assert calls["n"] == 0


def test_redeem_expired_cheap(station, monkeypatch):
    token, th = _mint(station, expires_in_days=-1)
    calls = _crypto_calls(monkeypatch)
    r = _do(station, token)
    assert not r.ok and r.status == redeem_mod.RedeemStatus.EXPIRED
    assert calls["n"] == 0


def test_redeem_bad_proof_increments_and_caps(station):
    token, th = _mint(station)
    bad = {"nonce": "n", "issued_at": "t", "signature_bundle": "not-json"}
    for _ in range(redeem_mod.REDEEM_ATTEMPT_CAP):
        r = redeem_mod.redeem(station, token, bad, ORIGIN, verifier=_binding_verifier())
        assert not r.ok and r.status == redeem_mod.RedeemStatus.PROOF_MALFORMED
    # cap hit -> next attempt rejected cheaply as rate-limited
    r = redeem_mod.redeem(station, token, bad, ORIGIN, verifier=_binding_verifier())
    assert r.status == redeem_mod.RedeemStatus.RATE_LIMITED


def test_redeem_idempotent_same_identity(station):
    token, th = _mint(station)
    r1 = _do(station, token)
    assert r1.ok and r1.status == redeem_mod.RedeemStatus.OK_REDEEMED
    r2 = _do(station, token)  # lost-ack retry, same identity
    assert r2.ok and r2.status == redeem_mod.RedeemStatus.OK_ALREADY
    assert (r2.issuer, r2.subject) == (ISSUER, SUBJECT)


def test_redeem_already_redeemed_by_other_identity(station):
    token, th = _mint(station)
    assert _do(station, token).ok
    # a different validly-signed identity presents a proof for the burned token
    other = redeem_mod.redeem(
        station, token, _proof(token, issuer=ISSUER, subject="mallory@example.com"),
        ORIGIN, verifier=_binding_verifier(issuer=ISSUER, subject="mallory@example.com"),
    )
    assert not other.ok and other.status == redeem_mod.RedeemStatus.ALREADY_REDEEMED


def test_redeem_revoked_identity_cannot_self_readmit(station):
    token, th = _mint(station)
    station.store.add_binding(ISSUER, SUBJECT, role="author")
    station.store.revoke_binding(ISSUER, SUBJECT)
    r = _do(station, token)
    assert not r.ok and r.status == redeem_mod.RedeemStatus.REVOKED_IDENTITY
    assert station.store.get_invite_by_token_hash(th)["used_at"] is None


def test_redeem_works_under_require_signed_off(station):
    # redeem is orthogonal to the publish gate — it binds regardless of the flag.
    token, th = _mint(station)
    r = _do(station, token)
    assert r.ok
    assert station.store.get_binding(ISSUER, SUBJECT) is not None


# --- INV-4: operator visibility ---------------------------------------------


def test_invite_list_shows_redeemed_by(station):
    token, th = _mint(station)
    _do(station, token)
    rows = station.store.list_invites()
    assert len(rows) == 1
    assert rows[0]["bound_subject"] == SUBJECT and rows[0]["redeemed_at"] is not None
    events = [e["event"] for e in station.store.get_invite_events(th)]
    assert events == ["minted", "redeemed"]


# --- e2e: the redeem ceremony through REAL signing.sign / verify_multi -------


def _unsigned_jwt(aud="sigstore", issuer=ISSUER) -> str:
    def b64(d):
        raw = json.dumps(d, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f'{b64({"alg": "none", "typ": "JWT"})}.{b64({"sub": SUBJECT, "iss": issuer, "aud": aud})}.'


@pytest.fixture
def real_provider():
    return signing.OIDCProviderConfig(issuer=ISSUER, token=_unsigned_jwt(), provider_id="google")


def test_e2e_real_signing_redeem_binds(station, real_provider, monkeypatch):
    # Proves the redeem ceremony plugs into the REAL signing primitive offline (no
    # network), the analogue of the publish E1 cell.
    _test_factory.install_sign_monkeypatch(monkeypatch, provider=real_provider)
    _test_factory.install_verify_monkeypatch(monkeypatch)
    token, th = _mint(station)
    signer = sign_mod.make_oidc_signer(real_provider, canon_profile=profile.CANON_PROFILE_REDEEM_V1)
    proof, issuer, subject = sign_mod.sign_redeem_proof(token, ORIGIN, signer)
    r = redeem_mod.redeem(station, token, proof, ORIGIN)  # real default_verifier
    assert r.ok and r.status == redeem_mod.RedeemStatus.OK_REDEEMED
    assert (r.issuer, r.subject) == (issuer, subject)
    b = station.store.get_binding(issuer, subject)
    assert b is not None and b.role == "author"


def test_e2e_real_harvested_manifest_bundle_cannot_redeem(station, real_provider, monkeypatch):
    # A REAL manifest bundle (the kind served publicly) presented on the redeem path
    # is 'wrong kind' — the cross-path pin, end-to-end through real signing.
    _test_factory.install_sign_monkeypatch(monkeypatch, provider=real_provider)
    _test_factory.install_verify_monkeypatch(monkeypatch)
    token, th = _mint(station)
    manifest_signer = sign_mod.make_oidc_signer(real_provider)  # MANIFEST profile
    ms = sign_mod.sign_manifest(["sha256::" + "0" * 64], manifest_signer)
    harvested = {"nonce": "n", "issued_at": "2026-01-01T00:00:00+00:00",
                 "signature_bundle": ms["signature_bundle"]}
    r = redeem_mod.redeem(station, token, harvested, ORIGIN)
    assert not r.ok and r.status == redeem_mod.RedeemStatus.PROOF_MALFORMED
    assert station.store.get_invite_by_token_hash(th)["used_at"] is None


def test_e2e_real_wrong_token_signature_mismatch(station, real_provider, monkeypatch):
    # Real crypto binding: a proof minted for token A cannot redeem token B.
    _test_factory.install_sign_monkeypatch(monkeypatch, provider=real_provider)
    _test_factory.install_verify_monkeypatch(monkeypatch)
    token_a, _ = _mint(station, token="tok-AAAA" + "a" * 36)
    token_b, _ = _mint(station, token="tok-BBBB" + "b" * 36)
    signer = sign_mod.make_oidc_signer(real_provider, canon_profile=profile.CANON_PROFILE_REDEEM_V1)
    proof_a, _, _ = sign_mod.sign_redeem_proof(token_a, ORIGIN, signer)
    # present token A's proof against token B
    r = redeem_mod.redeem(station, token_b, proof_a, ORIGIN)
    assert not r.ok and r.status == redeem_mod.RedeemStatus.PROOF_REJECTED


# --- INV-5: route hardening parity ------------------------------------------


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    d = tmp_path / "inst" / ".skein-next"
    s = Station(d)
    _mint(s, token="tok-route-" + "z" * 32)
    s.close()
    monkeypatch.setenv(ENV_DATA_DIR, str(d))
    monkeypatch.setenv(ENV_ORIGIN, ORIGIN)
    monkeypatch.delenv("SKEIN_NEXT_REQUIRE_SIGNED", raising=False)
    # the route uses the default verifier; swap it for the fake binding verifier
    monkeypatch.setattr(sign_mod, "default_verifier", _binding_verifier())
    return TestClient(create_app())


def _post(client, token, proof):
    return client.post(sign_mod.REDEEM_ROUTE, content=json.dumps({"token": token, "proof": proof}),
                       headers={"Content-Type": "application/json"})


def test_route_happy_200(app_client):
    token = "tok-route-" + "z" * 32
    r = _post(app_client, token, _proof(token))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["subject"] == SUBJECT


def test_route_oversized_413(app_client):
    payload = b'{"token":"' + b"x" * (REDEEM_MAX_BYTES + 1) + b'"}'
    r = app_client.post(sign_mod.REDEEM_ROUTE, content=payload, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_route_malformed_json_400(app_client):
    r = app_client.post(sign_mod.REDEEM_ROUTE, content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_route_deeply_nested_json_400(app_client):
    # Depth that blows Python's recursion limit (RecursionError, a RuntimeError, not
    # a ValueError) while the whole body stays UNDER the 64 KiB cap — so the parse,
    # not the size guard, is what rejects it. Proves the RecursionError catch.
    depth = 20000  # 40000 bytes < REDEEM_MAX_BYTES
    payload = b'{"token":' + b"[" * depth + b"]" * depth + b"}"
    assert len(payload) < REDEEM_MAX_BYTES
    r = app_client.post(sign_mod.REDEEM_ROUTE, content=payload, headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_route_missing_token_400(app_client):
    r = app_client.post(sign_mod.REDEEM_ROUTE, content=json.dumps({"proof": {}}),
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_route_unknown_token_409(app_client):
    r = _post(app_client, "tok-nope-" + "q" * 32, _proof("tok-nope-" + "q" * 32))
    assert r.status_code == 409
    assert r.json()["status"] == redeem_mod.RedeemStatus.UNKNOWN


def test_route_origin_unset_503(tmp_path, monkeypatch):
    d = tmp_path / "inst2" / ".skein-next"
    Station(d).close()
    monkeypatch.setenv(ENV_DATA_DIR, str(d))
    monkeypatch.delenv(ENV_ORIGIN, raising=False)
    monkeypatch.delenv("SKEIN_NEXT_REQUIRE_SIGNED", raising=False)
    client = TestClient(create_app())
    r = client.post(sign_mod.REDEEM_ROUTE, content=json.dumps({"token": "t", "proof": {}}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 503
