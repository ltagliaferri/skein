"""The mesh client core: resolve over HTTP, strict-verify locally, fork-F verdict.

Shared by the ``mesh fetch`` CLI and (later) the client-side MCP wrapper. The
verification here is the strict §4 path run on the CLIENT — it re-derives the
content hash from the ``body`` shown and checks the signature over the
domain-separated preimage. The station's ``asserted.verdict`` is the station's
word; this is the consumer re-deriving it, which is the whole point.
"""

from __future__ import annotations

import ipaddress
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

_LOOPBACK_HOSTS = frozenset({"localhost", "::1"})

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
    # Stderr-bound safety warnings (remote-unsigned, ref-head drift). Several can
    # apply to one fetch; they are newline-joined so the CLI's single echo prints
    # one warning per line.
    warning: Optional[str] = None
    # Did the served content's hash bind to the REQUESTED address?  True = the
    # address carried a digest and the content matched it; None = the address had
    # no digest to pin against locally (an alias/legacy id), so the name->hash
    # mapping is the station's word, not verified here.
    pinned: Optional[bool] = None
    # How it pinned: "full" (exact full-digest match) | "prefix" (short-hash
    # prefix bind, weaker) | None (unpinnable).
    pin_kind: Optional[str] = None


def _is_remote(instance: str) -> bool:
    host = (urlparse(instance).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return False
    # Only a genuine loopback IP LITERAL is local. The whole 127.0.0.0/8 range counts
    # (not just 127.0.0.1), as does IPv6 ::1 — ``ip_address().is_loopback`` covers both.
    # A hostname whose first label is "127" (e.g. "127.evil.example") is NOT an IP; it is
    # a registrable REMOTE domain, so a bare ``startswith("127.")`` would misclassify it
    # as local and suppress the remote-unsigned warning. A non-IP host that is not a
    # known loopback name is remote.
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def _pin_check(
    requested_address: Optional[str], actual_hash: str
) -> Tuple[Optional[bool], Optional[str], Optional[str]]:
    """Bind the requested address to the content's true identity (``actual_hash``).

    Content addressing's one invariant is address == hash(content); this is where
    the client enforces it against the station it does not trust. Returns
    ``(matched, kind, reason)``:

    - ``(True, "full", None)`` — a full-digest address, exact match.
    - ``(True, "prefix", None)`` — a short-digest address, the full hash extends
      it (a prefix bind: it constrains the station but is not full identity).
    - ``(False, None, reason)`` — the address carried a digest and the content did
      NOT match: the station served something other than what was asked for.
    - ``(None, None, reason)`` — the address has no locally-checkable digest (a
      bare alias or migrated legacy id), so the name->hash mapping is the
      station's word; the content self-certifies, it just isn't pinned here.
    """
    if not requested_address:
        return None, None, "no requested address to pin against"
    from .. import address as addr

    _unpinnable = (
        "requested address has no content-hash digest (alias/legacy id) — "
        "the name->hash mapping is the station's word, not verified locally"
    )
    try:
        parsed = addr.parse(requested_address)
    except addr.AddressError:
        return None, None, _unpinnable
    folio = parsed.folio
    # A Ref target (bare slug, ``ref::<slug>``, ``<name>::ref::<slug>``, …) has no
    # locally-checkable digest, and a ``#sha256::…`` fragment on a Ref is a FRESHNESS NOTE
    # — it warns, it does not verify or reject (ADDRESSING_GRAMMAR.md "The freshness
    # suffix"). So a Ref address is never a hard local pin: report it unpinnable (the
    # name->hash mapping is the station's word), and never touch ``.algo``/``.is_full`` on
    # a ``Ref``. The drift WARNING itself is :func:`fetch`'s job, via
    # :func:`_ref_drift_warning` — never this pin's, whose False return would reject.
    # (The hardened ``skein.address`` §3 grammar parses these bare-slug REF
    # forms that ``skein_next.address`` raised ``AddressError`` on; both routes land here
    # identically.)
    if isinstance(folio, addr.Ref):
        return None, None, _unpinnable
    # A hash target: the served content must match the folio digest AND, when the address
    # carries a ``#sha256::<full>`` verifier fragment, that digest too — on a hash target a
    # fragment ADDS a constraint, it never REPLACES the folio's (spec: "the resolved
    # object's content hash must equal it", reject on mismatch). Enforce EVERY digest-
    # bearing part; a full match anywhere is full identity, a folio-only short match is the
    # weaker prefix bind.
    algo, _sep, digest = actual_hash.partition("::")
    parts = [folio]
    if parsed.fragment is not None:
        parts.append(parsed.fragment)
    kind = "prefix"
    for part in parts:
        if part.algo != algo:
            return False, None, f"address mismatch: requested {part.algo}, served {algo}"
        if part.is_full:
            if part.digest != digest:
                return False, None, "address mismatch: served content does not hash to the requested address"
            kind = "full"
        elif not digest.startswith(part.digest):
            return False, None, "address mismatch: served hash does not extend the requested short hash"
    return True, kind, None


def _ref_drift_warning(requested_address: Optional[str], actual_hash: str) -> Optional[str]:
    """The freshness-note drift warning for a Ref address, or ``None``.

    On a REF address a ``#sha256::<digest>`` fragment is a FRESHNESS NOTE, not a
    pin (ADDRESSING_GRAMMAR.md "The freshness suffix"): resolution returns the
    current head, and this warns when that head has moved off the noted digest.
    It never rejects — a caller who wanted pin-or-fail writes the bare
    ``sha256::<digest>``, which :func:`_pin_check` enforces. The grammar admits
    exactly one fragment shape on a Ref (a full 64-hex sha256, enforced by
    ``address._parse_fragment`` + ``ParsedAddress.__post_init__``), so the match
    test is plain equality on algo + full digest — an algo mismatch is drift too
    (the head is not the noted digest), mirroring _pin_check's algo-prefix
    semantics without its short-hash prefix case (a fragment is never short).
    """
    if not requested_address:
        return None
    from .. import address as addr

    try:
        parsed = addr.parse(requested_address)
    except addr.AddressError:
        return None
    if not isinstance(parsed.folio, addr.Ref) or parsed.fragment is None:
        return None
    noted = f"{parsed.fragment.algo}::{parsed.fragment.digest}"
    algo, _sep, digest = actual_hash.partition("::")
    if parsed.fragment.algo == algo and parsed.fragment.digest == digest:
        return None  # head still at the noted digest — match is the quiet case
    return (
        f"warning: the head of {requested_address} has MOVED off the noted digest "
        f"({noted}; current head {actual_hash}) — you received the CURRENT head. "
        f"A freshness note does not pin; to pin-or-fail, re-resolve the bare {noted}."
    )


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


def verify_envelope(env: dict) -> Tuple[str, int, Optional[str], Optional[dict], Optional[str]]:
    """Strict, client-side verification of a resolved envelope's CONTENT (fork F).

    Returns ``(state, exit_code, reason, identity, content_hash)`` where
    ``content_hash`` is the hash RE-DERIVED from the body (the content's true
    identity), or ``None`` when the envelope did not verify. Binding that hash to
    the *requested address* is :func:`fetch`'s job (it holds the request), via
    :func:`_pin_check`.

    The content hash is ALWAYS re-derived locally and is the thing returned — the
    station's ``proof.content_hash`` is only cross-checked for consistency, never
    trusted as the identity. (A signed folio may legally omit ``content_hash``,
    since canon hashes only the body; returning the station's ``claimed`` there
    would let an omitted hash skip the address pin entirely.) The signature, when
    present, binds authorship to that same re-derived body.
    """
    # Hostile-station hardening: the response is untrusted JSON. A non-object
    # envelope, or a folio whose body/proof isn't an object (e.g. a collection's
    # list body returned at a folio address), is malformed — report it, never let
    # it crash the client (a `{**list}` / `.get` on a non-dict would).
    if not isinstance(env, dict):
        return "invalid", EXIT_SIGNATURE_INVALID, "malformed envelope (not an object)", None, None
    if env.get("kind") == "error":
        body = env.get("body")
        reason = body.get("error") if isinstance(body, dict) else None
        return "not_resolved", EXIT_NOT_RESOLVED, reason, None, None

    from .. import canon
    from ..identity import content_hash_for_bytes

    proof = env.get("proof")
    proof = proof if isinstance(proof, dict) else {}
    body = env.get("body")
    if not isinstance(body, dict):
        return "invalid", EXIT_SIGNATURE_INVALID, "malformed envelope (body is not an object)", None, None
    claimed = proof.get("content_hash")
    bundle = proof.get("signature_bundle")
    wire = {**body, "content_hash": claimed}

    # The content's true identity, re-derived from the body. This — not the
    # station's claim — is what gets pinned to the address. A hostile station
    # can serve a body with a field shape canon rejects (non-str type/title/
    # content/created_by, an unparseable created_at); that must fail closed as
    # "malformed body", not crash the client with a traceback.
    try:
        actual = content_hash_for_bytes(canon.folio_canonical_bytes(wire))
    except (canon.CanonError, ValueError, TypeError):
        return "invalid", EXIT_SIGNATURE_INVALID, "malformed envelope (invalid fields)", None, None

    # Cross-check: if the station claimed a hash, it must match the body it served.
    if claimed is not None and actual != claimed:
        return "invalid", EXIT_SIGNATURE_INVALID, "hash mismatch", None, None

    if bundle:
        from ..sign import verify_wire_folio  # lazy: keep Sigstore off unsigned reads

        wire["signature_bundle"] = json.dumps(bundle)
        verified, reason, identity = verify_wire_folio(wire)
        if verified:
            return "verified", EXIT_OK, "verified", identity, actual
        if reason in _VERIFIER_UNAVAILABLE:
            # Authorship uncheckable, but integrity still binds the body — return
            # the hash so fetch can still pin it to the requested address.
            return "unverified", EXIT_UNVERIFIED, reason, None, actual
        return "invalid", EXIT_SIGNATURE_INVALID, reason, None, None

    return "unsigned", EXIT_OK, None, None, actual


def fetch(
    instance: str,
    address: str,
    *,
    require_signed: bool = False,
    timeout: float = 10.0,
) -> FetchResult:
    """Resolve ``address`` against ``instance`` and strict-verify it (fork F).

    Verification is two checks the station cannot fake: the content is
    re-hashed and (if signed) the signature re-verified locally
    (:func:`verify_envelope`), and that content hash is then bound to the
    REQUESTED address (:func:`_pin_check`) — so a station serving content B under
    a request for address A is caught as an address mismatch, not reported clean.

    ``--require-signed`` turns a resolved-but-unsigned result into a non-zero
    exit. Remote-unsigned content additionally carries a stderr-bound warning (it
    is weak on both trust axes: remote + no authorship proof), as does a Ref
    freshness note whose head has drifted (:func:`_ref_drift_warning`) — warn,
    never reject, per ADDRESSING_GRAMMAR.md "The freshness suffix".
    """
    remote = _is_remote(instance)
    env, err = resolve(instance, address, timeout=timeout)
    if env is None:
        return FetchResult(
            address=address, instance=instance, resolved=False,
            state="not_resolved", exit_code=EXIT_NOT_RESOLVED, reason=err, remote=remote,
        )

    state, exit_code, reason, identity, content_hash = verify_envelope(env)
    pinned: Optional[bool] = None
    pin_kind: Optional[str] = None
    drift: Optional[str] = None

    # Bind the verified content to the address the caller asked for. Runs whenever
    # the content itself verified (content_hash is set) — including unverified
    # (authorship uncheckable but integrity-bound), so a substitution is caught
    # even when the signature can't be reached.
    if content_hash is not None:
        matched, kind, pin_reason = _pin_check(address, content_hash)
        if matched is False:
            state, exit_code, reason, identity = "invalid", EXIT_SIGNATURE_INVALID, pin_reason, None
        else:
            pinned = matched  # True (pinned) or None (no digest to pin against)
            pin_kind = kind
        # The Ref freshness note (warn-only; needs the same verified hash the pin
        # ran against). Changes NOTHING else — state/exit/pinned/pin_kind stay as
        # set above: a Ref remains unpinnable because the slug->head binding is
        # the station's word, drifted or not.
        drift = _ref_drift_warning(address, content_hash)

    resolved = state != "not_resolved"
    # Warning policy: both the remote-unsigned and the drift warning can apply to
    # one fetch (a drifted ref served unsigned by a remote). Neither clobbers the
    # other — collect and newline-join, keeping the pre-existing remote-unsigned
    # warning first so its position stays stable for anything scraping stderr.
    warnings = []
    if state == "unsigned":
        if require_signed:
            exit_code = EXIT_REQUIRE_SIGNED
        if remote:
            authority = urlparse(instance).hostname or instance
            warnings.append(
                f"warning: {address} is UNSIGNED and served by a remote instance "
                f"({authority}) — vouched only by that authority, no authorship proof."
            )
    if drift:
        warnings.append(drift)
    warning = "\n".join(warnings) if warnings else None

    # Never print an `invalid` (substituted / bad-signature) body to stdout — a
    # consumer reading stdout regardless of exit code must not ingest it. Only the
    # verdict (stderr) and exit code signal the failure.
    showable = state in ("verified", "unsigned", "unverified")
    markdown = _render(env) if showable else None
    return FetchResult(
        address=address, instance=instance, resolved=resolved, state=state,
        exit_code=exit_code, reason=reason, identity=identity, envelope=env,
        markdown=markdown, remote=remote, warning=warning, pinned=pinned, pin_kind=pin_kind,
    )


# --- display-trust path (the browse verbs + MCP) ----------------------------
#
# These fetch what the station RENDERED (agent markdown) and return it as-is —
# the display-trust convenience path (brief-20260603-dirz fork E). They do NOT
# verify; the rendering carries the addresses/bundle links so an agent can
# escalate to `mesh fetch` (resolve + strict verify) for a hard guarantee.
# Verification and federation never route through here.

_MARKDOWN_ACCEPT = "text/markdown"


def _fetch_text(instance: str, path: str, *, params: Optional[dict] = None, timeout: float = 10.0) -> str:
    """GET an agent-markdown rendering of a route; return the text (or an error line)."""
    url = instance.rstrip("/") + path
    try:
        resp = requests.get(url, params=params, headers={"Accept": _MARKDOWN_ACCEPT}, timeout=timeout)
    except requests.RequestException as e:
        return f"error: instance unreachable: {e}"
    return resp.text


def resolve_display(instance: str, address: str, *, timeout: float = 10.0) -> str:
    """A folio's agent-markdown rendering (display-trust; use ``fetch`` to verify)."""
    return _fetch_text(instance, f"/folio/{quote(address, safe='')}", timeout=timeout)


def search_display(instance: str, query: str, *, timeout: float = 10.0) -> str:
    """Search results as agent markdown."""
    return _fetch_text(instance, "/search", params={"q": query}, timeout=timeout)


def list_display(instance: str, slug: str, *, timeout: float = 10.0) -> str:
    """A site's folios as agent markdown."""
    return _fetch_text(instance, f"/site/{quote(slug, safe='')}", timeout=timeout)


def describe_display(instance: str, *, timeout: float = 10.0) -> str:
    """The station's describe document (the well-known root) as agent markdown."""
    return _fetch_text(instance, "/.well-known/skein", timeout=timeout)


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


def _pin_clause(result: FetchResult) -> str:
    """How the verified content relates to the address the caller asked for."""
    if result.pin_kind == "full":
        return "content hashes to the requested address"
    if result.pin_kind == "prefix":
        return "served hash extends the requested short hash (prefix bind, not full identity)"
    return (
        "could not pin to the requested address locally (alias/legacy id) — "
        "the name->hash mapping is the station's word"
    )


def verdict_line(result: FetchResult) -> str:
    """A one-line human verdict for stderr, built from the LOCAL verification.

    The ``invalid`` state covers both a bad signature and an address mismatch (the
    station served content other than what was requested); the ``reason`` says
    which. ``verified``/``unsigned`` carry whether the content pinned to the
    requested address — an unpinned result is stated honestly, never as clean.
    """
    if result.state == "verified":
        subject = (result.identity or {}).get("subject") or "verified"
        issuer = (result.identity or {}).get("issuer")
        who = f"{subject} ({issuer})" if issuer else subject
        return f"VERIFIED — signed by {who}; {_pin_clause(result)}"
    if result.state == "unsigned":
        return f"UNSIGNED — no authorship proof; {_pin_clause(result)}"
    if result.state == "unverified":
        return (
            f"UNVERIFIED — signature present but verifier unavailable ({result.reason}); "
            f"{_pin_clause(result)}"
        )
    if result.state == "invalid":
        return f"INVALID — {result.reason}"
    return f"NOT RESOLVED — {result.reason or 'no folio at that address'}"
