"""Authz issuer regression — issue-20260619-oufc.

The bug: operator bootstrapped under accounts.google.com but a human Sigstore
login goes through the Dex broker (oauth2.sigstore.dev/auth), so can_write()
on the exact (issuer, subject) pair rejects the operator's own signed publish.

These two tests would have caught that:

- BROKER_ACCEPTED: bootstrap under the broker issuer, sign under the broker →
  can_write() returns True, publish accepted.
- GOOGLE_BOOTSTRAP_BROKER_SIGN_REJECTED: bootstrap under accounts.google.com,
  sign under the broker → (issuer, subject) pair mismatch → can_write() False,
  publish rejected with "unbound signer".

The second test locks the exact-pair match semantics AND proves the doc fix
isn't just cosmetic: a binding under the wrong issuer still fails.
"""

from __future__ import annotations

import base64
import json

import pytest

from skein import signing
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus, _test_factory

from skein_next import profile, wire
from skein_next import sign as sign_mod
from skein_next import publish as pub_mod
from skein_next.ingress import ingest
from skein_next.station import Station

BROKER = "https://oauth2.sigstore.dev/auth"
GOOGLE = "https://accounts.google.com"
ALICE = "alice@example.com"


def _signer(issuer: str, subject: str = ALICE):
    def s(canonical_bytes: bytes) -> sign_mod.SignedResult:
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, canonical_bytes)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=["x"],
            canonical_bytes=preimage,
            canon_version=profile.CANON_PROFILE_MANIFEST_V1,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)
    return s


def _verifier_for(issuer: str, subject: str = ALICE):
    def v(canonical_bytes: bytes, bundle) -> MultiVerifyResult:
        return MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=issuer, subject=subject)],
            overall=VerifyStatus.VERIFIED,
        )
    return v


def _seed_and_batch(client: Station, signer) -> dict:
    client.create_site("specs", purpose="p", created_by="t")
    client.post("finding", "specs", "T", "b", created_by="t")
    folios, threads, slugs = pub_mod.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in batch["folios"]] + [t["thread_hash"] for t in batch["threads"]]
    batch["manifest_signature"] = sign_mod.sign_manifest(addrs, signer)
    return batch


@pytest.fixture
def client(tmp_path):
    s = Station(tmp_path / "client" / ".skein-next")
    yield s
    s.close()


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein-next")
    yield s
    s.close()


def test_broker_bootstrap_and_sign_accepted(client, instance):
    """Operator bootstrapped under broker, signing under broker → accepted.

    This is the canonical production path: `interskein login` returns a token
    with issuer=broker; the operator must bootstrap under that same issuer.
    """
    instance.store.add_binding(BROKER, ALICE, role="operator",
                               vouched_by_issuer=BROKER, vouched_by_subject=ALICE)
    batch = _seed_and_batch(client, _signer(issuer=BROKER))
    ack = ingest(instance, batch, verifier=_verifier_for(BROKER), require_signed=True)
    assert ack["accepted"], "operator's own signed publish was rejected under require_signed"
    assert ack["rejected"] == [], f"unexpected rejections: {ack['rejected']}"


def test_google_bootstrap_broker_sign_rejected(client, instance):
    """Operator bootstrapped under accounts.google.com, signing under broker → rejected.

    This is the bug: the binding is under Google but the Sigstore cert carries
    the broker issuer. can_write() keys on the exact (issuer, subject) pair, so
    the mismatch must reject with 'unbound signer' — not silently accept.
    """
    instance.store.add_binding(GOOGLE, ALICE, role="operator",
                               vouched_by_issuer=GOOGLE, vouched_by_subject=ALICE)
    batch = _seed_and_batch(client, _signer(issuer=BROKER))
    ack = ingest(instance, batch, verifier=_verifier_for(BROKER), require_signed=True)
    assert ack["accepted"] == [], "mismatch should not have been accepted"
    assert all(r["reason"] == "unbound signer" for r in ack["rejected"]), (
        f"expected 'unbound signer', got: {[r['reason'] for r in ack['rejected']]}"
    )


# ---------------------------------------------------------------------------
# Helpers for the two new guard tests below.
# ---------------------------------------------------------------------------


def _make_jwt(aud: str = "sigstore", issuer: str = BROKER) -> str:
    """Unsigned JWT with aud and iss claims — enough to pass signing.sign()'s guard."""
    def _b64(d: dict) -> str:
        raw = json.dumps(d, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    header = _b64({"alg": "none", "typ": "JWT"})
    payload = _b64({"aud": aud, "iss": issuer, "sub": ALICE})
    return f"{header}.{payload}."


# ---------------------------------------------------------------------------
# Guard test 1: the _build_bundle DEFAULT (no explicit issuer/provider)
# ---------------------------------------------------------------------------


def test_default_issuer_is_broker_accepted(client, instance, monkeypatch):
    """_build_bundle's no-issuer/no-provider fallback must be the broker, not Google.

    The fix flipped signing._SIGSTORE_PROD_BROKER from accounts.google.com to the
    broker. This test exercises that fallback: install_sign_monkeypatch is called
    with provider=None so _build_bundle opts["provider"] is None and no opts["issuer"]
    is present. _build_bundle then falls back to _SIGSTORE_PROD_BROKER — the cert it
    mints carries that issuer and sign() extracts it for SignedResult.issuer. We bind
    ONLY the broker on the instance; if the fallback were still google, the extracted
    issuer would be google, the binding lookup would miss, and ingest would reject with
    'unbound signer' — so the test fails on a revert.

    Routing through ingest (rather than just inspecting the cert) is chosen because
    it closes the full loop: default issuer → cert → verify extraction → binding lookup
    → accept/reject. A cert-only assertion would not catch a verifier that re-maps the
    issuer before the binding check.
    """
    # install_sign_monkeypatch with provider=None: _sign_state["provider"] = None,
    # so _build_bundle falls through to the _SIGSTORE_PROD_BROKER default.
    _test_factory.install_sign_monkeypatch(monkeypatch, provider=None)
    _test_factory.install_verify_monkeypatch(monkeypatch)

    # OIDCProviderConfig with broker issuer passes sign()'s allowlist + aud checks;
    # the factory ignores the provider's issuer when building the cert (provider=None
    # in _sign_state means _build_bundle uses the _SIGSTORE_PROD_BROKER literal).
    broker_provider = signing.OIDCProviderConfig(
        issuer=BROKER,
        token=_make_jwt(issuer=BROKER),
        provider_id=None,
    )
    signer = sign_mod.make_oidc_signer(broker_provider)

    instance.store.add_binding(BROKER, ALICE, role="operator",
                               vouched_by_issuer=BROKER, vouched_by_subject=ALICE)

    batch = _seed_and_batch(client, signer)
    ack = ingest(instance, batch, require_signed=True)
    assert ack["accepted"], (
        "publish rejected — the cert issuer from _build_bundle default does not "
        "match the broker binding; check signing._SIGSTORE_PROD_BROKER default"
    )
    assert ack["rejected"] == [], f"unexpected rejections: {ack['rejected']}"


# ---------------------------------------------------------------------------
# Guard test 2: the CLI --oidc-issuer DEFAULT
# ---------------------------------------------------------------------------


def test_cli_publish_oidc_issuer_default_is_broker():
    """The publish CLI --oidc-issuer option must default to SIGSTORE_PROD_ISSUER.

    Guards cli.py's _BROKER_ISSUER default against drift back to accounts.google.com.
    A wrong default would silently supply the wrong issuer to an operator who doesn't
    pass --oidc-issuer explicitly, producing a cert that no correctly-bootstrapped
    instance would accept.
    """
    from skein_next.cli import publish as publish_cmd

    issuer_param = next(
        (p for p in publish_cmd.params if p.name == "oidc_issuer"),
        None,
    )
    assert issuer_param is not None, "--oidc-issuer param not found on publish command"
    assert issuer_param.default == sign_mod.SIGSTORE_PROD_ISSUER, (
        f"--oidc-issuer default is {issuer_param.default!r}, expected "
        f"SIGSTORE_PROD_ISSUER={sign_mod.SIGSTORE_PROD_ISSUER!r}"
    )
