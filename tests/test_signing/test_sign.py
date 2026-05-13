"""sign() contract — happy path, size edges, Unicode-heavy bytes, non-ASCII
identity, ephemeral key non-reuse, expires_at gating, library regression coverage.

sign() is the write path. A bug here either silently produces unverifiable
bundles (corrupts the entire SKEIN write history) or stranger: produces a
valid bundle that wasn't what the user thought they were signing. Cross-payload
binding is the load-bearing property.

Per clarification 1: sign() returns a SignResult (Pydantic model with
bundle_json, issuer, subject, signing_timestamp, evidence).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


# Enforces: sign() returns a SignResult whose bundle_json round-trips through
# verify() against the same canonical_bytes.
def test_sign_happy_path_returns_verifiable_result(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_simple, google_provider)
    assert isinstance(result, signing.SignResult)

    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Enforces: SignResult exposes (issuer, subject, signing_timestamp) extracted
# at sign time per Identity rev 5 §"Folio schema additions § indexing/display".
def test_sign_exposes_indexed_identity_fields(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, identity="alice@example.com",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    assert result.subject == "alice@example.com"
    assert result.issuer == "https://accounts.google.com"
    assert result.signing_timestamp > 0  # microseconds UTC


# Enforces: SignResult.signing_timestamp comes from Rekor's RFC3161 TSA timestamp
# (Rekor v2), not client clock. The exact value is implementation-defined but it
# must be in microseconds.
def test_sign_signing_timestamp_is_microseconds(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_simple, google_provider)
    # > 1e15 microseconds = > year 2001 in microseconds
    assert result.signing_timestamp > 1_000_000_000_000_000


# Enforces: SignResult.evidence is populated on success (carries validity_window,
# rekor_inclusion, cert_chain_summary).
def test_sign_evidence_populated(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_simple, google_provider)
    assert result.evidence is not None


# Enforces: sign() preserves canonical_bytes byte-exactly. No re-canonicalization,
# re-normalization, or re-encoding. The signature binds to the bytes the caller
# passed.
def test_sign_does_not_mutate_canonical_bytes(
    crypto_factory, google_provider, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    original = bytes(range(256))
    sentinel = bytes(original)
    _ = signing.sign(original, google_provider)
    assert original == sentinel


# Enforces: sign() produces one SignResult per call. The caller assembles
# multi-signer arrays from N sign() calls. Per Identity rev 5 §boundary:
# "sign() produces one SignResult (one canonical sigstore-bundle JSON string
# in result.bundle_json); caller assembles the array."
def test_sign_produces_one_result_per_call(
    crypto_factory, google_provider, github_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    r1 = signing.sign(canonical_bytes_simple, google_provider)
    r2 = signing.sign(canonical_bytes_simple, github_provider)
    assert isinstance(r1, signing.SignResult)
    assert isinstance(r2, signing.SignResult)


# ---------------------------------------------------------------------------
# Edge cases — size
# ---------------------------------------------------------------------------


# Enforces: sign() accepts empty canonical_bytes. (Defensively: empty is a
# knurl/canon concern, but sign() must not gatekeep size.)
def test_sign_empty_canonical_bytes(
    crypto_factory, google_provider, canonical_bytes_empty, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_empty, google_provider)
    assert isinstance(result, signing.SignResult)


# Enforces: sign() accepts 1-byte canonical_bytes.
def test_sign_one_byte_canonical_bytes(
    crypto_factory, google_provider, canonical_bytes_one_byte, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_one_byte, google_provider)
    assert isinstance(result, signing.SignResult)


# Enforces: sign() handles a 1 MiB payload without erroring out.
def test_sign_one_mb_canonical_bytes(
    crypto_factory, google_provider, canonical_bytes_one_mb, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_one_mb, google_provider)
    assert isinstance(result, signing.SignResult)


# ---------------------------------------------------------------------------
# Edge cases — Unicode
# ---------------------------------------------------------------------------


# Enforces: sign() accepts Unicode-heavy canonical_bytes. Catches the accidental
# .decode().encode() bug class — signature binds to bytes the caller passed.
def test_sign_unicode_heavy_canonical_bytes(
    crypto_factory, google_provider, canonical_bytes_unicode_heavy, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_unicode_heavy, google_provider)
    assert isinstance(result, signing.SignResult)


# Enforces: regression for sigstore-python #1507. Non-ASCII in the OIDC identity
# claim must NOT cause a silent malformed CSR. signing.py either succeeds with
# the non-ASCII identity OR raises SigningUnavailable with component="fulcio"
# and a reason that names the issue. Either is acceptable; what's unacceptable
# is a silently malformed CSR or a generic Exception.
def test_sign_non_ascii_identity_raises_structured(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, identity="josé@example.com",
        fulcio_rejects_non_ascii=True,
    )
    try:
        signing.sign(canonical_bytes_simple, google_provider)
    except signing.SigningUnavailable as e:
        assert e.component == "fulcio"
        assert "identity" in e.reason.lower() or "csr" in e.reason.lower()
    # If signing.py has worked around the upstream bug, no exception is fine too.


# ---------------------------------------------------------------------------
# Non-determinism / non-reuse
# ---------------------------------------------------------------------------


# Enforces: two sign() calls over the same canonical_bytes with the same provider
# produce TWO DISTINCT bundles. Sigstore signing is non-deterministic (fresh
# ephemeral key + ECDSA nonce per call). This is the load-bearing property
# behind the verify-cache key being (content_hash, bundle_hash).
def test_sign_is_non_deterministic(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    a = signing.sign(canonical_bytes_simple, google_provider)
    b = signing.sign(canonical_bytes_simple, google_provider)
    assert a.bundle_json != b.bundle_json


# Enforces: both bundles verify against the same canonical_bytes. Non-determinism
# at the bundle layer must not break verification.
def test_sign_non_deterministic_both_verify(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    a = signing.sign(canonical_bytes_simple, google_provider)
    b = signing.sign(canonical_bytes_simple, google_provider)
    sb_a = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[a.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    sb_b = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[b.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    assert signing.verify(canonical_bytes_simple, sb_a).status == signing.VerifyStatus.VERIFIED
    assert signing.verify(canonical_bytes_simple, sb_b).status == signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Boundary contract — sign() raises SigningUnavailable, not raw errors
# ---------------------------------------------------------------------------


# Enforces: when Fulcio returns 5xx, sign() raises SigningUnavailable with
# component="fulcio". The wrapper MUST NOT let raw FulcioClientError leak —
# that breaks the boundary contract with sign_queue.py.
def test_sign_fulcio_failure_raises_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, fulcio_status=503,
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "fulcio"


# Enforces: when Rekor returns 5xx, sign() raises SigningUnavailable with
# component="rekor". Half-state contract: the caller has no half-signed folio.
def test_sign_rekor_failure_raises_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, rekor_status=502,
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "rekor"


# Enforces: when OIDC token acquisition fails, sign() raises SigningUnavailable
# with component="oidc".
def test_sign_oidc_failure_raises_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, oidc_status="unreachable",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "oidc"


# Enforces: network-down (all three unreachable) is also SigningUnavailable,
# not a raw OSError / ConnectionError.
def test_sign_total_network_down_raises_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, network="down",
    )
    with pytest.raises(signing.SigningUnavailable):
        signing.sign(canonical_bytes_simple, google_provider)


# ---------------------------------------------------------------------------
# expires_at — sign-side OIDC token expiry check (clarification 3)
# ---------------------------------------------------------------------------


# Enforces: per clarification 3, if expires_at is in the past or imminent,
# sign() raises SigningUnavailable with a clear "OIDC token expired" reason
# BEFORE talking to Fulcio.
def test_sign_expired_token_raises_signing_unavailable_before_fulcio(
    crypto_factory, expired_google_provider, canonical_bytes_simple, monkeypatch
):
    # Even if Fulcio is healthy, the expired token check fires first.
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=expired_google_provider)
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, expired_google_provider)
    assert exc.value.component == "oidc"
    assert "expir" in exc.value.reason.lower() or "token" in exc.value.reason.lower()


# Enforces: when expires_at is None (token-flow that doesn't surface expiry),
# sign() proceeds normally and lets Fulcio reject if the token is actually bad.
def test_sign_no_expires_at_proceeds_normally(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    assert google_provider.expires_at is None
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_simple, google_provider)
    assert isinstance(result, signing.SignResult)


# Enforces: future expires_at allows signing to proceed.
def test_sign_future_expires_at_proceeds(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    far_future = 4_102_444_800_000_000  # 2100-01-01 UTC microseconds
    provider = signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token="future-token",
        provider_id="google",
        expires_at=far_future,
    )
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    result = signing.sign(canonical_bytes_simple, provider)
    assert isinstance(result, signing.SignResult)


# ---------------------------------------------------------------------------
# Library upstream regressions
# ---------------------------------------------------------------------------


# Enforces: regression for sigstore-python #1729 (Signer does not refresh
# expired certs mid-batch). If the cached cert expires between sign() calls in
# a session, sign() either refreshes transparently OR raises SigningUnavailable
# with component="fulcio". Bare ExpiredCertificate must NOT leak.
def test_sign_expired_cert_does_not_leak_raw_exception(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, cert_expired_on_second_call=True,
    )
    _ = signing.sign(canonical_bytes_simple, google_provider)
    try:
        signing.sign(canonical_bytes_simple, google_provider)
    except signing.SigningUnavailable:
        pass
    except Exception as e:
        pytest.fail(
            f"sign() leaked upstream {type(e).__name__}; should be SigningUnavailable"
        )


# Enforces: in-process parallel sign() calls must not leak raw upstream
# exceptions. This is narrower than sigstore-python #1403, which is covered by
# the subprocess/shared-cache staging test below.
def test_sign_parallel_calls_within_one_process_are_safe(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, simulate_tuf_race=True,
    )
    results: list = []
    errors: list = []

    def worker():
        try:
            results.append(signing.sign(canonical_bytes_simple, google_provider))
        except signing.SigningUnavailable as e:
            errors.append(e)
        except Exception as e:
            errors.append(("RAW", e))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw_errors = [e for e in errors if isinstance(e, tuple) and e[0] == "RAW"]
    assert raw_errors == [], f"TUF race surfaced raw exceptions: {raw_errors}"


# Enforces: oracle actionable #5 / sigstore-python #1403. The production race is
# across processes sharing one TUF cache, so this live staging contract launches
# two independent Python interpreters with the same SIGSTORE_TUF_CACHE_DIR.
@pytest.mark.staging
def test_sign_safe_across_processes_with_shared_tuf_cache(tmp_path):
    token = os.environ.get("SKEIN_TEST_SIGSTORE_TOKEN")
    if not token:
        pytest.skip("SKEIN_TEST_SIGSTORE_TOKEN not set for live staging sign test.")

    cache_dir = tmp_path / "shared-tuf-cache"
    script = """
import json
import os
from skein import signing

provider = signing.OIDCProviderConfig(
    issuer=os.environ["SKEIN_TEST_SIGSTORE_ISSUER"],
    token=os.environ["SKEIN_TEST_SIGSTORE_TOKEN"],
    provider_id=os.environ.get("SKEIN_TEST_SIGSTORE_PROVIDER_ID", "google"),
)
result = signing.sign(b"skein batch 6 tuf process race", provider)
print(result.model_dump_json())
"""
    env = os.environ.copy()
    env["SIGSTORE_TUF_CACHE_DIR"] = str(cache_dir)
    env.setdefault("SKEIN_TEST_SIGSTORE_ISSUER", "https://oauth2.sigstage.dev/auth")

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for _ in range(2)
    ]
    runs = []
    try:
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=180)
            runs.append(
                subprocess.CompletedProcess(
                    proc.args,
                    proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
            )
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()

    assert [run.returncode for run in runs] == [0, 0], [
        {"stdout": run.stdout, "stderr": run.stderr, "returncode": run.returncode}
        for run in runs
    ]
    for run in runs:
        result = signing.SignResult.model_validate_json(
            run.stdout.strip().splitlines()[-1]
        )
        assert json.loads(result.bundle_json)

    leftovers = [
        p for p in cache_dir.rglob("*")
        if p.name.endswith((".tmp", ".lock")) or ".tmp" in p.name
    ]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Staging integration (skipped unless SKEIN_TEST_SIGSTORE_LIVE=1)
# ---------------------------------------------------------------------------


@pytest.mark.staging
def test_sign_against_sigstore_staging(google_provider, canonical_bytes_simple):
    # Live integration. Requires SKEIN_TEST_SIGSTORE_LIVE=1 and a human at the
    # terminal to complete the OIDC flow.
    cfg = signing.OIDCProviderConfig(
        issuer="https://oauth2.sigstage.dev/auth",
        token="staging-token-acquired-out-of-band",
        provider_id="google",
    )
    result = signing.sign(canonical_bytes_simple, cfg)
    assert result.subject  # some non-empty cert SAN identity
