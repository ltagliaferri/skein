"""The mesh client core: resolve over HTTP, strict-verify locally, fork-F verdict.

Shared by the ``mesh fetch`` CLI and (later) the client-side MCP wrapper. The
verification here is the strict §4 path run on the CLIENT — it re-derives the
content hash from the ``body`` shown and checks the signature over the
domain-separated preimage. The station's ``asserted.verdict`` is the station's
word; this is the consumer re-deriving it, which is the whole point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import quote, urlparse

import requests

DEFAULT_INSTANCE = "http://127.0.0.1:9001"

# verify_multi statuses meaning "the verifier could not check", NOT "the signature
# is bad" — mirror of envelope._VERIFIER_UNAVAILABLE. These read as UNVERIFIED, a
# distinct exit, so a transient trust-root problem never reads as forgery.
_VERIFIER_UNAVAILABLE = frozenset({"OFFLINE_NO_TRUSTED_ROOT", "TRUST_ROOT_STALE"})

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# --- fork F exit codes ------------------------------------------------------
EXIT_OK = 0  # resolved + verified, or resolved + unsigned (without --require-signed)
EXIT_NOT_RESOLVED = 2  # no folio at that address, or the instance was unreachable
EXIT_SIGNATURE_INVALID = 3  # signature bad, or the body doesn't match its address
EXIT_UNVERIFIED = 4  # a signature is present but the verifier couldn't be reached
EXIT_REQUIRE_SIGNED = 5  # resolved + unsigned, but --require-signed demanded a signature


@dataclass
class FetchResult:
    """The outcome of resolving + verifying one address against an instance."""

    address: str
    instance: str
    resolved: bool
    state: str  # verified | unsigned | invalid | unverified | not_resolved
    exit_code: int
    reason: Optional[str] = None
    identity: Optional[dict] = None
    envelope: Optional[dict] = None
    markdown: Optional[str] = None
    remote: bool = False
    warning: Optional[str] = None


def _is_remote(instance: str) -> bool:
    host = (urlparse(instance).hostname or "").lower()
    return host not in _LOOPBACK_HOSTS


def resolve(instance: str, address: str, *, timeout: float = 10.0) -> Tuple[Optional[dict], Optional[str]]:
    """GET the JSON envelope for ``address`` from ``instance``.

    Returns ``(envelope, error)``: the parsed envelope (a folio OR a station
    error envelope) on a reachable instance, or ``(None, message)`` when the
    instance is unreachable or returns unparseable content — which the caller
    treats as not-resolved, not as a station error envelope.
    """
    url = f"{instance.rstrip('/')}/folio/{quote(address, safe='')}.json"
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as e:
        return None, f"instance unreachable: {e}"
    try:
        return resp.json(), None
    except ValueError:
        return None, f"instance returned non-JSON (HTTP {resp.status_code})"


def verify_envelope(env: dict) -> Tuple[str, int, Optional[str], Optional[dict]]:
    """Strict, client-side verification of a resolved envelope (fork F).

    Returns ``(state, exit_code, reason, identity)``. An error envelope is
    not-resolved. A folio is verified over its ``proof`` + ``body``: a signed
    folio runs the full §4 path; an unsigned folio is still integrity-checked
    (the content hash must bind the body shown) so a station that serves a body
    not matching its claimed address is caught as invalid, never reported clean.
    """
    if env.get("kind") == "error":
        reason = (env.get("body") or {}).get("error")
        return "not_resolved", EXIT_NOT_RESOLVED, reason, None

    proof = env.get("proof") or {}
    body = env.get("body") or {}
    claimed = proof.get("content_hash")
    bundle = proof.get("signature_bundle")
    wire = {**body, "content_hash": claimed}

    if bundle:
        from ..sign import verify_wire_folio  # lazy: keep Sigstore off unsigned reads

        wire["signature_bundle"] = json.dumps(bundle)
        verified, reason, identity = verify_wire_folio(wire)
        if verified:
            return "verified", EXIT_OK, "verified", identity
        if reason in _VERIFIER_UNAVAILABLE:
            return "unverified", EXIT_UNVERIFIED, reason, None
        return "invalid", EXIT_SIGNATURE_INVALID, reason, None

    # Unsigned: integrity-level proof. Re-derive the hash and confirm it binds the
    # body shown — the station's word is not trusted even for the hash.
    from .. import canon
    from ..identity import content_hash_for_bytes

    if claimed and content_hash_for_bytes(canon.folio_canonical_bytes(wire)) != claimed:
        return "invalid", EXIT_SIGNATURE_INVALID, "hash mismatch", None
    return "unsigned", EXIT_OK, None, None


def fetch(
    instance: str,
    address: str,
    *,
    require_signed: bool = False,
    timeout: float = 10.0,
) -> FetchResult:
    """Resolve ``address`` against ``instance`` and strict-verify it (fork F).

    ``--require-signed`` turns a resolved-but-unsigned result into a non-zero
    exit. Remote-unsigned content additionally carries a stderr-bound warning (it
    is weak on both trust axes: remote + no authorship proof).
    """
    remote = _is_remote(instance)
    env, err = resolve(instance, address, timeout=timeout)
    if env is None:
        return FetchResult(
            address=address, instance=instance, resolved=False,
            state="not_resolved", exit_code=EXIT_NOT_RESOLVED, reason=err, remote=remote,
        )

    state, exit_code, reason, identity = verify_envelope(env)
    resolved = state != "not_resolved"
    warning = None
    if state == "unsigned":
        if require_signed:
            exit_code = EXIT_REQUIRE_SIGNED
        if remote:
            authority = urlparse(instance).hostname or instance
            warning = (
                f"warning: {address} is UNSIGNED and served by a remote instance "
                f"({authority}) — vouched only by that authority, no authorship proof."
            )

    markdown = _render(env) if resolved else None
    return FetchResult(
        address=address, instance=instance, resolved=resolved, state=state,
        exit_code=exit_code, reason=reason, identity=identity, envelope=env,
        markdown=markdown, remote=remote, warning=warning,
    )


def _render(env: dict) -> str:
    """The agent-markdown rendering of a resolved envelope, for display."""
    from .. import render as render_mod

    if env.get("kind") == "folio":
        text, _nonce = render_mod.render_folio_markdown(env)
        return text
    if env.get("kind") == "error":
        return render_mod.render_error_markdown(env)
    text, _nonce = render_mod.render_collection_markdown(env, title=env.get("address", ""))
    return text


def verdict_line(result: FetchResult) -> str:
    """A one-line human verdict for stderr, built from the LOCAL verification."""
    if result.state == "verified":
        subject = (result.identity or {}).get("subject") or "verified"
        issuer = (result.identity or {}).get("issuer")
        who = f"{subject} ({issuer})" if issuer else subject
        return f"VERIFIED — signed by {who}"
    if result.state == "unsigned":
        return "UNSIGNED — integrity verified (content hash binds the body); no authorship proof"
    if result.state == "unverified":
        return f"UNVERIFIED — signature present but verifier unavailable ({result.reason})"
    if result.state == "invalid":
        return f"SIGNATURE INVALID — {result.reason}"
    return f"NOT RESOLVED — {result.reason or 'no folio at that address'}"
