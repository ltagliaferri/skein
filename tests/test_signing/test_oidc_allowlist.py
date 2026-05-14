"""OIDC issuer allowlist contract (finding-20260513-w5hq section 2)."""
from __future__ import annotations

import pytest

pytest.importorskip("skein.signing")

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


def test_sign_rejects_issuer_not_in_v0_allowlist(
    crypto_factory, make_oidc_provider, canonical_bytes_simple, monkeypatch,
):
    provider = make_oidc_provider(
        issuer="https://login.microsoftonline.com/common/v2.0", provider_id="azure",
    )
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, provider)
    assert exc.value.component == "oidc"
    assert "allowlist" in exc.value.reason.lower()


def test_sign_rejects_github_actions_oidc_issuer(
    crypto_factory, make_oidc_provider, canonical_bytes_simple, monkeypatch,
):
    provider = make_oidc_provider(
        issuer="https://token.actions.githubusercontent.com", provider_id="github-actions",
    )
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, provider)
    assert exc.value.component == "oidc"
    assert "allowlist" in exc.value.reason.lower()


def test_sign_accepts_google_personal_oauth(
    crypto_factory, make_oidc_provider, canonical_bytes_simple, monkeypatch,
):
    provider = make_oidc_provider(issuer="https://accounts.google.com", provider_id="google")
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    result = signing.sign(canonical_bytes_simple, provider)
    assert isinstance(result, signing.SignResult)


def test_sign_accepts_github_personal_oauth(
    crypto_factory, make_oidc_provider, canonical_bytes_simple, monkeypatch,
):
    provider = make_oidc_provider(issuer="https://github.com/login/oauth", provider_id="github")
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    result = signing.sign(canonical_bytes_simple, provider)
    assert isinstance(result, signing.SignResult)
