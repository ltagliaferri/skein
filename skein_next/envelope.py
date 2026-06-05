"""The unified wire envelope (brief-20260603-ujwx rev 2).

One frame, a typed ``body``, built straight from the native content-hash store —
NOT the legacy ``ContentHashAdapter``/``FolioView``/``cross_refs`` path, which
reshapes the store into the old ``Folio`` model and loses thread direction. The
machine surface serves the native, verifiable model; HTML is one downstream
rendering of it (slice 3).

The frame (§2)::

    {
      "schema":    "skein.envelope/v1",
      "address":   "<resolved address>",
      "kind":      "folio | site | search | catalog | error",
      "stability": "stable | derived",
      "as_of":     "<iso8601>",          # required for derived; null for stable
      "body":      <kind-specific>,
      "proof":     {profile, content_hash, signature_bundle} | null,
      "asserted":  {...station claims...},
      "links":     {...control-frame breadcrumbs...},
      "next":      "<address> | null"
    }

Two trust tiers live in one object and must stay visibly separate (§1, §7):
``proof`` is the verifiable spine (content_hash binds ``body``; a signature, when
present, binds authorship); everything in ``asserted`` is the station's *word*
(status, site, graph edges, and its own ``verdict`` line) and is never to be
trusted without the consumer re-deriving it. The control frame
(``schema``/``address``/``kind``/``stability``/``as_of``/``links``/``next``) is
station-rendered structure — derivable from ``body`` or query-time — so it is
unsigned by design (§4).

Phase 1 builds against the existing, mostly-unsigned corpus at the ``integrity``
proof level: ``content_hash`` binds the body, ``signature_bundle`` is ``null``
for unsigned folios. The two live signed docs carry their bundle and the
station's display verdict; the strict, domain-separated verification path and
re-signing under the profile are slice 2.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

SCHEMA = "skein.envelope/v1"

# The signed-preimage profile (§3): names the trust domain, object kind, and
# canonicalization version in one token. At integrity level it still names the
# canonicalization that produced ``content_hash``; at authorship level it is the
# string bound into the signature (slice 2).
CANON_PROFILE = "skein.folio.canon/v1"

# The five canonical folio fields, in nothing-but-these order (canon sorts keys;
# this is for readers and for slicing a folio row down to its body).
BODY_FIELDS = ("type", "title", "content", "created_at", "created_by")

# Thread edges that are station structure, not cross-references: membership and
# status. Excluded from the folio graph, same rule the adapter's cross_refs uses.
_STRUCTURAL_THREADS = frozenset({"within", "status"})

# A folio is stable (full content hash, immutable); a collection or error is a
# query result, derived. ``validate_envelope`` enforces this mapping.
_STABLE_KINDS = frozenset({"folio", "thread"})
_DERIVED_KINDS = frozenset({"site", "search", "catalog", "error"})

# verify_multi statuses meaning "the verifier could not check", NOT "the
# signature is bad" — these must read as UNVERIFIED, never SIGNATURE INVALID, so
# a transient trust-root problem never slanders a legitimately signed folio.
_VERIFIER_UNAVAILABLE = frozenset({"OFFLINE_NO_TRUSTED_ROOT", "TRUST_ROOT_STALE"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_envelope(env: Mapping[str, Any]) -> Dict[str, Any]:
    """Enforce the kind->stability->proof invariants; return the dict unchanged.

    A malformed envelope (wrong stability for its kind, a stable envelope with no
    proof, a derived one carrying proof or missing ``as_of``) is a construction
    bug, not bad input — so this raises ``ValueError`` rather than producing a
    quietly-wrong frame.
    """
    kind = env.get("kind")
    stability = env.get("stability")
    if kind in _STABLE_KINDS:
        expected = "stable"
    elif kind in _DERIVED_KINDS:
        expected = "derived"
    else:
        raise ValueError(f"unknown envelope kind: {kind!r}")
    if stability != expected:
        raise ValueError(f"kind {kind!r} requires stability {expected!r}, got {stability!r}")

    if stability == "stable":
        if env.get("proof") is None:
            raise ValueError(f"stable {kind!r} envelope must carry a proof")
    else:
        if env.get("proof") is not None:
            raise ValueError(f"derived {kind!r} envelope must have proof: null")
        if not env.get("as_of"):
            raise ValueError(f"derived {kind!r} envelope must carry as_of")
    return dict(env)


# --- folio ------------------------------------------------------------------


def folio_verdict(
    store,
    content_hash: str,
    row: Mapping[str, Any],
    bundle_json: Optional[str] = None,
) -> tuple[str, Optional[Dict[str, Optional[str]]]]:
    """The station's provenance verdict line for a folio, and the signer identity.

    This is an ``asserted`` claim — the station's word, to be re-derived by a
    careful consumer, never trusted as fact. Unsigned content is honestly
    operator-vouched (integrity only). For a signed folio it runs the display
    verification path (same one the HTML provenance card uses), distinguishing a
    bad signature from a verifier that could not be reached. The slice-2 strict,
    domain-separated path replaces the call without changing this contract.

    ``bundle_json`` may be passed by a caller that already read the sidecar (the
    envelope builder), so a folio read does one signature fetch, not two.
    """
    if bundle_json is None:
        bundle_json = store.get_signature(content_hash)
    if not bundle_json:
        return ("UNSIGNED — operator-vouched, not cryptographically signed", None)

    from .sign import verify_wire_folio  # lazy: keep Sigstore off unsigned reads

    wire_folio = {**row, "signature_bundle": bundle_json}
    verified, reason, identity = verify_wire_folio(wire_folio)
    if verified:
        subject = (identity or {}).get("subject")
        return (f"SIGNED — {subject or 'verified'} (verified)", identity)
    if reason in _VERIFIER_UNAVAILABLE:
        return (f"UNVERIFIED — verifier unavailable ({reason})", None)
    return (f"SIGNATURE INVALID — {reason}", None)


def _folio_href(content_hash: str) -> str:
    return f"/folio/{content_hash}"


def _folio_threads(store, content_hash: str, *, outgoing: bool) -> List[Dict[str, Any]]:
    """The folio's cross-reference edges in one direction, native and labelled.

    Outgoing = edges this folio is the ``from_id`` of; incoming = the ``to_id``.
    Membership/status edges are excluded (they surface as ``site``/``status``).
    Each entry carries the thread type, the peer's title, the peer's full
    ``address`` (the cross-instance-safe handle), and a station-local ``href``
    (which 404s for a peer whose only instance is elsewhere — following uses the
    address, not the href).

    A folio is not its own neighbour: a self-edge is dropped whether the peer is
    the folio's own hash OR a legacy id that aliases back to it.
    """
    edges = (
        store.get_threads(from_id=content_hash)
        if outgoing
        else store.get_threads(to_id=content_hash)
    )
    out: List[Dict[str, Any]] = []
    seen = set()
    for edge in edges:
        if edge["type"] in _STRUCTURAL_THREADS:
            continue
        peer = edge["to_id"] if outgoing else edge["from_id"]
        if not peer or peer == content_hash:
            continue
        # Resolve the endpoint: a content hash directly, else a legacy id through
        # the alias table. ``target`` is None when the peer is held nowhere local.
        target = peer if store.get_folio(peer) else store.resolve_alias(peer)
        if target == content_hash:
            continue  # aliases back to this folio — not its own neighbour
        prow = store.get_folio(target) if target else None
        if prow is not None:
            address = prow["content_hash"]
            title = prow.get("title") or ""
            href = _folio_href(address)
        else:
            # A peer with no local instance: still a real edge. Expose the raw
            # endpoint as the address so a federating client can chase it.
            address = peer
            title = None
            href = _folio_href(peer)
        key = (edge["type"], address)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": edge["type"], "title": title, "address": address, "href": href})
    return out


def _folio_site(store, content_hash: str) -> Optional[Dict[str, Any]]:
    slug = store.folio_site_slug(content_hash)
    if not slug:
        return None
    site_hash = store.resolve_slug(slug)
    return {"slug": slug, "address": site_hash, "href": f"/site/{slug}"}


def build_folio_envelope(
    store,
    content_hash: str,
    *,
    address: Optional[str] = None,
    row: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the unified envelope for a folio from the native store.

    ``content_hash`` must already exist (the caller's resolve+get_folio is the
    existence gate). ``address`` is the canonical address to echo back (defaults
    to the bare content hash; a configured ``web::`` authority supplies the
    federated form in a later slice). ``row`` may be passed to avoid a re-read.
    """
    if row is None:
        row = store.get_folio(content_hash)
        if row is None:
            raise KeyError(content_hash)

    body = {field: row.get(field) for field in BODY_FIELDS}

    bundle_json = store.get_signature(content_hash)
    signature_bundle = _bundle_object(bundle_json)
    verdict, _identity = folio_verdict(store, content_hash, row, bundle_json)

    links = {
        "self": _folio_href(content_hash),
        "raw": f"{_folio_href(content_hash)}.md",
        "catalog": "/",
    }
    if signature_bundle is not None:
        links["bundle"] = f"{_folio_href(content_hash)}/bundle"

    env = {
        "schema": SCHEMA,
        "address": address or content_hash,
        "kind": "folio",
        "stability": "stable",
        "as_of": None,
        "body": body,
        "proof": {
            "profile": CANON_PROFILE,
            "content_hash": content_hash,
            "signature_bundle": signature_bundle,
        },
        "asserted": {
            "verdict": verdict,
            "status": store.latest_statuses([content_hash]).get(content_hash, "open"),
            "site": _folio_site(store, content_hash),
            "threads_out": _folio_threads(store, content_hash, outgoing=True),
            "threads_in": _folio_threads(store, content_hash, outgoing=False),
        },
        "links": links,
        "next": None,
    }
    return validate_envelope(env)


def _bundle_object(bundle_json: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse the stored bundle JSON text into the wire object form (§3), or None.

    The store holds the bundle as ``SignatureBundle.model_dump_json`` text; on the
    wire it is presented as a JSON object. A malformed sidecar is surfaced as
    unsigned-with-no-object rather than crashing the read (the verdict line, built
    separately, still reports the malformed bundle).
    """
    if not bundle_json:
        return None
    import json

    try:
        return json.loads(bundle_json)
    except (ValueError, TypeError):
        return None


# --- collections (catalog / site / search) ----------------------------------


def folio_entry(row: Mapping[str, Any], *, snippet: Optional[str] = None) -> Dict[str, Any]:
    """A collection entry pointing at a folio: an address that feeds resolve.

    Entries are the station's table of contents — discovery, not verifiable. The
    ``snippet`` is untrusted content (it goes inside the fence when rendered).
    """
    content_hash = row["content_hash"]
    return {
        "address": content_hash,
        "kind": "folio",
        "type": row.get("type") or "folio",
        "title": row.get("title") or "",
        "snippet": snippet,
        "href": _folio_href(content_hash),
    }


def build_collection_envelope(
    kind: str,
    address: str,
    entries: List[Dict[str, Any]],
    *,
    asserted: Optional[Dict[str, Any]] = None,
    links: Optional[Dict[str, Any]] = None,
    next_address: Optional[str] = None,
) -> Dict[str, Any]:
    """A derived envelope over a list of entries (catalog/site/search).

    ``proof`` is null and ``as_of`` is stamped: the items it points at are each
    independently verifiable, but the completeness of the *set* is only the
    station's word at this instant (§5). ``next_address`` is a cursor-address for
    pagination (a later slice; the field exists so the shape is stable).
    """
    env = {
        "schema": SCHEMA,
        "address": address,
        "kind": kind,
        "stability": "derived",
        "as_of": _now_iso(),
        "body": entries,
        "proof": None,
        "asserted": asserted or {},
        "links": links or {"catalog": "/"},
        "next": next_address,
    }
    return validate_envelope(env)


# --- error / absence --------------------------------------------------------


def build_error_envelope(
    error: str,
    address: str,
    *,
    origin: Optional[str] = None,
) -> Dict[str, Any]:
    """A machine-parseable miss/error envelope — never an English sentence (§6).

    A 404 from a federating cache distinguishes "origin returned 404" from "never
    existed here" via ``links.origin``.
    """
    links: Dict[str, Any] = {"catalog": "/"}
    if origin:
        links["origin"] = origin
    env = {
        "schema": SCHEMA,
        "address": address,
        "kind": "error",
        "stability": "derived",
        "as_of": _now_iso(),
        "body": {"found": False, "error": error},
        "proof": None,
        "asserted": {},
        "links": links,
        "next": None,
        "suggestion": "mesh fetch <address>",
    }
    return validate_envelope(env)
