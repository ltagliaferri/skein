"""Phase 4: the working skein's publish capability. Design: docs/PHASE_4_DESIGN.md.

A curlable, author-DECLARED publish. Assemble a declared set of folios + threads,
sign an RFC-6962 Merkle manifest over their content hashes, and POST it to a remote
station's ingress. This module is the pure logic + the ported crypto/wire wrappers;
the API route (skein/routes.py) and the thin CLI wrapper (client/cli.py) sit on top.

Reuses skein.canon (Merkle + descriptor) and skein.signing (Sigstore) — the crypto
already present on the working-server side. The manifest signer / wire / ingress
client are ported thin from the abandoned skein_next reference (canon and signing are
byte-identical forks there; one-shared-module consolidation is a follow-up, gate
§10 #1). Do NOT import the working server from skein_next.

The model (gate §5): the author declares the set; the ONLY vetoes are physics
(integrity — bytes reproduce the hash). Type is advisory — the linter WARNS
(dangling / slug-endpoint / looks-local) but never blocks. compute_thread_hash and
the canon hashing are untouched.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from . import canon
from .identity import compute_folio_hash, compute_thread_hash

# --- domain separation (ported from skein_next/profile.py) ------------------
CANON_PROFILE_MANIFEST_V1 = "skein.manifest.canon/v1"
_SEPARATOR = b"\x00"

# Absolute DoS cap on a manifest's leaf set — one source of truth shared by the
# signer side (fail before the irreversible Sigstore ceremony) and the verifier.
# Sized for the public write surface: a real publish is a handful of leaves.
MAX_LEAVES = 2048

# Bookkeeping edge that never travels (a folio->instance publish-state ledger).
PUBLISHED_THREAD = "published"

# Heuristic sets for the advisory "looks-local" lint ONLY (NOT a gate — the author
# may deliberately publish any of these; gate §5.1/§5.4).
_CONTROL_LIKE = frozenset({"status", "assignment", "archive"})
_COMMENTARY = frozenset({"tag", "message"})

_SHA256_ADDRESS_RE = re.compile(r"^sha256::([0-9a-f]{64})$")
_BARE_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def profiled_preimage(profile: str, canonical_bytes: bytes) -> bytes:
    """``profile || NUL || canonical_bytes`` — the signed/verified preimage."""
    return profile.encode("utf-8") + _SEPARATOR + canonical_bytes


# --- address helpers --------------------------------------------------------
def is_content_address(value: Optional[str]) -> bool:
    """True iff ``value`` is a resolvable content hash (``sha256::<64hex>`` or bare
    64-hex). A slug / agent id is not — it means nothing on another station."""
    if not isinstance(value, str):
        return False
    return bool(_SHA256_ADDRESS_RE.match(value) or _BARE_HEX_RE.match(value))


def to_leaf_address(value: str) -> str:
    """Normalize a content hash to the framed ``sha256::<hex>`` leaf-address form.

    Both the bare-hex and framed forms map to the same leaf; a non-content value is
    a caller error (leaf addresses are folio content hashes and thread hashes, which
    are always content-addressed)."""
    if _SHA256_ADDRESS_RE.match(value):
        return value
    if _BARE_HEX_RE.match(value):
        return "sha256::" + value
    raise ValueError(f"not a content address: {value!r}")


def manifest_leaf_addresses(
    folios: Sequence[Mapping[str, Any]], threads: Sequence[Mapping[str, Any]]
) -> List[str]:
    """The manifest leaf set: every folio ``content_hash`` ++ every thread
    ``thread_hash``, framed as ``sha256::<hex>`` (kind-agnostic, gate §7)."""
    out = [to_leaf_address(f["content_hash"]) for f in folios]
    out += [to_leaf_address(t["thread_hash"]) for t in threads]
    return out


# --- the proposer (reachability SUGGESTION, gate §5.2) ----------------------
def propose_reachable(
    folio_hashes: Set[str],
    incident_threads: Sequence[Mapping[str, Any]],
    already_on: Set[str] = frozenset(),
) -> List[Dict[str, Any]]:
    """The default proposal: edges among the selected folios (the demoted
    ``_closed_threads`` reachability — both endpoints in ``folio_hashes | already_on``,
    excluding the ``published`` bookkeeping edge). A SUGGESTION the author edits, not
    a filter: it excludes dangling edges (the author may add those explicitly, and the
    linter then warns). De-duplicated by thread_hash, insertion order preserved."""
    eligible = set(folio_hashes) | set(already_on)
    seen: Dict[str, Dict[str, Any]] = {}
    for t in incident_threads:
        if t.get("type") == PUBLISHED_THREAD:
            continue
        if t.get("from_id") in eligible and t.get("to_id") in eligible:
            seen.setdefault(t["thread_hash"], dict(t))
    return list(seen.values())


# --- the linter (advisory — WARNS, never blocks; gate §5.3) -----------------
def _warn(code: str, subject: str, message: str) -> Dict[str, str]:
    return {"code": code, "subject": subject, "message": message}


def lint_declared_set(
    folios: Sequence[Mapping[str, Any]], threads: Sequence[Mapping[str, Any]]
) -> List[Dict[str, str]]:
    """Advisory warnings over the declared set. NEVER blocks a publish (gate §5.3):

    - ``dangling``: a declared edge whose endpoint is a content hash but that folio
      is not in the declared set (resolves lazily elsewhere).
    - ``slug-endpoint``: an edge endpoint that is not a content hash — won't resolve
      off-station.
    - ``looks-local``: a control/commentary self-loop (status/tag/...) — usually
      local, but publishable on purpose (the "closed on the public station" path).
    """
    # TOTAL — the linter is advisory and must NEVER raise (a malformed row is the
    # physics veto's job, not the linter's): skip non-mapping / hashless rows here.
    declared = {
        to_leaf_address(f["content_hash"]) for f in folios
        if isinstance(f, Mapping) and is_content_address(f.get("content_hash"))
    }
    warnings: List[Dict[str, str]] = []
    for t in threads:
        if not isinstance(t, Mapping):
            continue
        th = t.get("thread_hash", "?")
        ttype = t.get("type")
        frm, to = t.get("from_id"), t.get("to_id")
        if frm == to and (ttype in _CONTROL_LIKE or ttype in _COMMENTARY):
            warnings.append(_warn(
                "looks-local", th,
                f"{ttype} self-loop is usually local state; publishing it is deliberate"))
        for role, ep in (("from_id", frm), ("to_id", to)):
            if ep is None:
                continue
            if not is_content_address(ep):
                warnings.append(_warn(
                    "slug-endpoint", th,
                    f"{role} {ep!r} is not a content hash; it won't resolve off-station"))
            elif to_leaf_address(ep) not in declared:
                # a content-hash endpoint (incl. a self-loop) whose folio is not in
                # the publish is dangling — the frm!=to guard was a false-negative
                warnings.append(_warn(
                    "dangling", th,
                    f"{role} {ep} points to a folio not in this publish"))
    return warnings


# --- the physics floor (the ONLY veto — fail closed; gate §5.5) -------------
class PhysicsError(ValueError):
    """A declared constituent's bytes do not reproduce its stated content hash."""


def _bare(h: str) -> str:
    m = _SHA256_ADDRESS_RE.match(h)
    return m.group(1) if m else h


def _stated_hash(row: Mapping[str, Any], key: str) -> str:
    """The row's stated content hash, or a typed PhysicsError if the row is not a
    mapping, or the hash is missing / not a content address (so a dropped column, a
    non-found lookup represented as None, or a hostile body fails CLOSED and CLEANLY,
    never as a raw KeyError/TypeError/AttributeError 500 in the route)."""
    if not isinstance(row, Mapping):
        raise PhysicsError(f"declared row is not a mapping: {row!r}")
    h = row.get(key)
    if not is_content_address(h):
        raise PhysicsError(f"{key} missing or not a content hash: {h!r}")
    return h  # type: ignore[return-value]


def physics_check(
    folios: Sequence[Mapping[str, Any]], threads: Sequence[Mapping[str, Any]]
) -> None:
    """Recompute every folio's and thread's content hash from its canonical bytes and
    refuse the publish if any stated hash does not reproduce (gate §5.5). Uses the
    unchanged skein.identity hashers, so the manifest can never sign a body under a
    hash it does not actually have.

    TOTAL over a malformed declared row (missing/None/non-str field) — every failure
    is a typed :class:`PhysicsError`, mirroring the receiver's total reject-reason, so
    the publish route fails closed with a clean 4xx instead of a 500."""
    for f in folios:
        stated = _stated_hash(f, "content_hash")
        try:
            got = compute_folio_hash(f)
        except Exception as e:  # canon rejects non-str/None fields -> typed refusal
            raise PhysicsError(f"folio not hashable: {e}") from e
        if _bare(got) != _bare(stated):
            raise PhysicsError(
                f"folio hash mismatch: stated {stated!r}, recomputed {got!r}")
    for t in threads:
        stated = _stated_hash(t, "thread_hash")
        try:
            got = compute_thread_hash(
                t.get("from_id"), t.get("to_id"), t.get("type"),
                t.get("weaver"), t.get("created_at"), t.get("content"))
        except Exception as e:
            raise PhysicsError(f"thread not hashable: {e}") from e
        if _bare(got) != _bare(stated):
            raise PhysicsError(
                f"thread hash mismatch: stated {stated!r}, recomputed {got!r}")


# --- the Merkle manifest (ported from skein_next/sign.py, over skein.canon) --
@dataclass(frozen=True)
class SignedResult:
    """A manifest signer's return: the bundle plus the VERIFIED cert identity."""
    bundle: Any
    issuer: str
    subject: str


# A (manifest) Signer maps a descriptor's canonical bytes to a SignedResult.
Signer = Callable[[bytes], SignedResult]


def build_manifest(constituent_addresses: List[str]) -> Dict[str, Any]:
    """``{"root", "leaf_count", "leaf_list"}`` over a constituent-hash set — leaves
    sorted+deduped by raw datum, root framed, count distinct (kind-agnostic)."""
    data_to_addr: Dict[bytes, str] = {}
    for addr in constituent_addresses:
        data_to_addr[canon.address_to_leaf_datum(addr)] = addr
    leaf_list = [data_to_addr[d] for d in sorted(data_to_addr)]
    root = canon.merkle_root_for_addresses(leaf_list)
    return {"root": root, "leaf_count": len(leaf_list), "leaf_list": leaf_list}


def sign_manifest(constituent_addresses: List[str], manifest_signer: Signer) -> Dict[str, Any]:
    """Sign ONE descriptor over a publish's whole constituent set. Returns the wire
    ``manifest_signature``. Signed bytes are the descriptor only; ``leaf_list`` rides
    unsigned so the receiver can recompute the root."""
    # Count DISTINCT raw leaf data, exactly as the signed leaf_count and the remote
    # verifier's cap do (dedup_leaf_count), so the signer never over-rejects a batch
    # the verifier would accept on a framing difference (sha256::<h> vs bare <h>).
    if canon.dedup_leaf_count(constituent_addresses) > MAX_LEAVES:
        raise ValueError(f"manifest exceeds MAX_LEAVES ({MAX_LEAVES})")
    manifest = build_manifest(constituent_addresses)
    descriptor_bytes = canon.manifest_descriptor_canonical_bytes(
        manifest["root"], manifest["leaf_count"])
    result = manifest_signer(descriptor_bytes)
    bundle = result.bundle
    bundle_json = bundle.model_dump_json() if hasattr(bundle, "model_dump_json") else json.dumps(bundle)
    return {
        "descriptor": {"root": manifest["root"], "leaf_count": manifest["leaf_count"]},
        "leaf_list": manifest["leaf_list"],
        "signature_bundle": bundle_json,
        "issuer": result.issuer,
        "subject": result.subject,
    }


def make_oidc_signer(oidc_provider: Any, identity_scheme: str = "sigstore-public-v1",
                     trust_root_pin: Optional[str] = None,
                     canon_profile: str = CANON_PROFILE_MANIFEST_V1) -> Signer:
    """A manifest Signer that signs a descriptor under ``oidc_provider`` via Sigstore.
    The interactive 1-click login that produces the provider/token is the caller's
    (gate §6). Imports skein.signing lazily so this module loads without the signing
    stack for the pure-logic paths."""
    from . import signing

    def _sign(canonical_bytes: bytes) -> SignedResult:
        preimage = profiled_preimage(canon_profile, canonical_bytes)
        result = signing.sign(preimage, oidc_provider)
        bundle = signing.SignatureBundle(
            identity_scheme=identity_scheme,
            bundles=[result.bundle_json],
            canonical_bytes=preimage,
            canon_version=canon_profile,
            trust_root_pin=trust_root_pin,
        )
        return SignedResult(bundle=bundle, issuer=result.issuer, subject=result.subject)

    return _sign


def _issuer_from_jwt(token: str) -> Optional[str]:
    """The ``iss`` claim from a JWT's (UNVERIFIED) payload — used only to route the
    provider. The token's signature is verified for real inside signing.sign via
    Fulcio; reading ``iss`` here is just addressing, not trust."""
    import base64
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        return json.loads(base64.urlsafe_b64decode(payload)).get("iss")
    except Exception:
        return None


def signer_from_token(token: str, issuer: Optional[str] = None) -> Signer:
    """Build a manifest Signer from an OIDC ``token`` obtained by a prior 1-click login
    (gate §6). When ``issuer`` is not given it is read from the token's own ``iss``
    claim (the token is self-describing) — OIDCProviderConfig requires a real issuer
    string. The publish route hands its token here; the interactive login is a separate
    client step. Tests inject a fake signer in place of this."""
    from .signing import OIDCProviderConfig

    resolved_issuer = issuer or _issuer_from_jwt(token)
    provider = OIDCProviderConfig(issuer=resolved_issuer, token=token, provider_id=None)
    return make_oidc_signer(provider)


# --- the wire batch (ported from skein_next/wire.py) ------------------------
PROTOCOL = "skein-publish/v0"
_FOLIO_WIRE_FIELDS = ("content_hash", "type", "title", "content", "created_at", "created_by")
_THREAD_WIRE_FIELDS = ("thread_hash", "from_id", "to_id", "type", "weaver", "created_at", "content")


def folio_to_wire(row: Mapping[str, Any]) -> Dict[str, Any]:
    d = {k: row.get(k) for k in _FOLIO_WIRE_FIELDS}
    # created_at from the store is a datetime; the wire must be JSON-serializable AND
    # re-hash identically, so normalize to the SAME canonical UTC-isoformat string the
    # hash is computed over (idempotent on the receiver).
    d["created_at"] = canon.normalize_created_at(d.get("created_at"))
    return d


def thread_to_wire(row: Mapping[str, Any]) -> Dict[str, Any]:
    d = {k: row.get(k) for k in _THREAD_WIRE_FIELDS}
    d["created_at"] = canon.normalize_created_at(d.get("created_at"))
    return d


def build_batch(folios: Sequence[Mapping[str, Any]], threads: Sequence[Mapping[str, Any]],
                site_slugs: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "folios": [folio_to_wire(f) for f in folios],
        "threads": [thread_to_wire(t) for t in threads],
        "site_slugs": dict(site_slugs or {}),
    }


# --- the ingress client (ported from skein_next/publish.py) -----------------
class PublishError(RuntimeError):
    """A transport failure or a non-2xx from the remote ingress."""


def canonical_instance(url: str) -> str:
    return url.rstrip("/")


def post_batch(instance_url: str, batch: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    """POST a publish batch to a remote instance's ingress; return the parsed ack."""
    endpoint = canonical_instance(instance_url) + "/publish/v0/folios"
    body = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise PublishError(f"instance rejected publish ({e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise PublishError(f"could not reach instance at {endpoint}: {e.reason}") from e
    except ValueError as e:
        # a schemeless / malformed instance url makes urlopen raise a raw ValueError
        # (not URLError); surface it as a typed transport failure, never a 500.
        raise PublishError(f"invalid instance url {endpoint!r}: {e}") from e


# --- the all-up orchestrator (the ONLY way the route should build a publish) --
def assemble_signed_batch(
    folios: Sequence[Mapping[str, Any]],
    threads: Sequence[Mapping[str, Any]],
    site_slugs: Optional[Dict[str, str]],
    signer: Signer,
) -> Dict[str, Any]:
    """physics_check -> leaves -> sign_manifest -> build_batch -> attach the manifest.

    The single composed path, so a caller cannot post a batch with the signature
    forgotten (the fell's abuse case): physics runs FIRST (a malformed declared row
    raises PhysicsError BEFORE the signer's irreversible Sigstore ceremony), and the
    returned batch always carries ``manifest_signature``."""
    physics_check(folios, threads)
    leaves = manifest_leaf_addresses(folios, threads)
    manifest_signature = sign_manifest(leaves, signer)
    batch = build_batch(folios, threads, site_slugs)
    batch["manifest_signature"] = manifest_signature
    return batch


def publish(
    instance_url: str,
    folios: Sequence[Mapping[str, Any]],
    threads: Sequence[Mapping[str, Any]],
    signer: Signer,
    site_slugs: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Assemble the signed batch and POST it to ``instance_url``'s ingress; return the
    ack. Destination-agnostic by design (gate §6): the signature names no target, so a
    signer cannot restrict which stations carry content they have signed."""
    batch = assemble_signed_batch(folios, threads, site_slugs, signer)
    return post_batch(instance_url, batch)
