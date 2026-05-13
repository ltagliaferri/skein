"""Fixtures for SKEIN signing conformance tests.

All corpus bundles are from sigstore-python's test/assets/ (MIT-licensed).
They are staging bundles: signed against staging.sigstore.dev, verifiable
via Verifier.staging(offline=True) using the TUF root baked into sigstore-python.

This is intentional: the corpus ships with sigstore-python, not with SKEIN.
SKEIN's production bundles use Verifier.production(), but there is no
portable pre-signed corpus for production infrastructure, so we use staging
for conformance assertions.

References: finding-20260511-2ysh (§Test fixtures and conformance corpus),
finding-20260511-qejb, brief-20260511-nbz4 (rev 5 spec).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

CORPUS_DIR = Path(__file__).parent / "corpus"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_artifact(stem: str, subdir: str = "") -> bytes:
    """Read the signed artifact bytes for a corpus bundle."""
    base = CORPUS_DIR / subdir if subdir else CORPUS_DIR
    return (base / f"{stem}.txt").read_bytes()


def _read_bundle_json(stem: str, subdir: str = "", suffix: str = ".sigstore") -> str:
    """Read the raw sigstore-bundle ProtoJSON string for a corpus bundle."""
    base = CORPUS_DIR / subdir if subdir else CORPUS_DIR
    filename = f"{stem}.txt{suffix}"
    return (base / filename).read_bytes().decode("utf-8")


def _read_whl_bundle_json(stem: str) -> str:
    """Read the raw sigstore-bundle ProtoJSON for a .whl corpus bundle."""
    return (CORPUS_DIR / f"{stem}.whl.sigstore").read_bytes().decode("utf-8")


def make_skein_bundle(
    canonical_bytes: bytes,
    bundle_blobs: list[str],
    identity_scheme: str = "sigstore-public-v1",
    canon_version: str = "knurl-1.0",
    trust_root_pin: str | None = None,
) -> dict[str, Any]:
    """Assemble a SKEIN signature_bundle dict from constituent parts.

    This mirrors what SKEIN's write path produces after calling sign():
    canonical_bytes are base64-encoded, bundles is a list of raw
    sigstore-bundle ProtoJSON strings (one canonical sigstore-bundle JSON
    string per signer).
    """
    bundle: dict[str, Any] = {
        "identity_scheme": identity_scheme,
        "bundles": bundle_blobs,
        "canonical_bytes": base64.b64encode(canonical_bytes).decode("ascii"),
        "canon_version": canon_version,
    }
    if trust_root_pin is not None:
        bundle["trust_root_pin"] = trust_root_pin
    return bundle


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def corpus_dir() -> Path:
    return CORPUS_DIR


@pytest.fixture(scope="session")
def make_bundle():
    """Factory: make a SKEIN signature_bundle from corpus stem + blob."""
    return make_skein_bundle


@pytest.fixture(scope="session")
def bundle_v3():
    """Known-good v0.3 staging bundle."""
    artifact = _read_artifact("bundle_v3")
    blob = _read_bundle_json("bundle_v3")
    return artifact, make_skein_bundle(artifact, [blob])


@pytest.fixture(scope="session")
def bundle_v3_alt():
    """Known-good v0.3 staging bundle, alternate signer."""
    artifact = _read_artifact("bundle_v3_alt")
    blob = _read_bundle_json("bundle_v3_alt")
    return artifact, make_skein_bundle(artifact, [blob])


@pytest.fixture(scope="session")
def bundle_tsa():
    """Known-good v0.3 staging bundle with RFC3161 TSA timestamps (Rekor v2 path)."""
    artifact = _read_artifact("bundle", subdir="tsa")
    blob = _read_bundle_json("bundle", subdir="tsa")
    return artifact, make_skein_bundle(artifact, [blob])


@pytest.fixture(scope="session")
def bundle_cve_2022_36056():
    """CVE-2022-36056 replay bundle: tlog entry inconsistent with bundle material."""
    artifact = _read_artifact("bundle_cve_2022_36056")
    blob = _read_bundle_json("bundle_cve_2022_36056")
    return artifact, make_skein_bundle(artifact, [blob])


@pytest.fixture(scope="session")
def bundle_invalid_version():
    """Bundle with completely invalid mediaType: 'this is completely wrong'."""
    artifact = _read_artifact("bundle_invalid_version")
    blob = _read_bundle_json("bundle_invalid_version")
    return artifact, make_skein_bundle(artifact, [blob])


@pytest.fixture(scope="session")
def bundle_no_cert_v1():
    """v0.1-era bundle using x509CertificateChain (full chain) instead of certificate (leaf only)."""
    artifact = _read_artifact("bundle_no_cert_v1")
    blob = _read_bundle_json("bundle_no_cert_v1")
    return artifact, make_skein_bundle(artifact, [blob])


@pytest.fixture(scope="session")
def bundle_no_checkpoint():
    """v0.2-era bundle with SET-only tlog entry: no inclusionProof.checkpoint.

    Represents a Rekor v1 bundle. sigstore-public-v1 MUST reject this because
    it requires checkpoint-backed inclusion proofs (Rekor v2 requirement per rev 5 spec).
    """
    artifact = _read_artifact("bundle_no_checkpoint")
    blob = _read_bundle_json("bundle_no_checkpoint")
    return artifact, make_skein_bundle(artifact, [blob])


@pytest.fixture(scope="session")
def bundle_no_log_entry():
    """v0.1-era bundle with empty tlogEntries: no transparency log proof at all."""
    artifact = _read_artifact("bundle_no_log_entry")
    blob = _read_bundle_json("bundle_no_log_entry")
    return artifact, make_skein_bundle(artifact, [blob])


@pytest.fixture(scope="session")
def bundle_v3_no_signed_time():
    """v0.3 bundle with no inclusionPromise (no SET).

    Valid per v0.3 spec when an inclusion proof is present. Tests that
    SKEIN does not require the legacy SET field.
    """
    artifact = _read_artifact("bundle_v3_no_signed_time")
    blob = _read_bundle_json(
        "bundle_v3_no_signed_time", suffix=".sigstore.json"
    )
    return artifact, make_skein_bundle(artifact, [blob])
