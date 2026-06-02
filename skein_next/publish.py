"""Client-side publish: push selected folios from a local station to an instance.

PROTOTYPE — unsigned. This is the write side of the client/instance split
(finding-20260521-kwtb): a client gathers a closed set of local folios and pushes
them to an instance's ingress, which stores them and serves them read-only. The
publish step is the boundary where signing will later happen (brief-20260522-q8k0);
for now the batch crosses unsigned, protected only by the content-hash integrity
check the ingress performs.

Selection and the closed-graph rule
-----------------------------------
You publish a whole site (``--site SLUG``) or specific folios (refs). Either way
the set is closed before it goes:

- the site folio itself travels (as a ``type=site`` folio), so the instance can
  reconstruct membership against the same site hash rather than minting a new one;
- a thread is included only when BOTH its endpoints are *eligible*, where
  eligible = (in this batch) OR (already published to this same instance). That
  keeps ``within`` membership and ``status`` self-loops (endpoints in the batch),
  and it makes dropped edges CONVERGENT: an edge to a folio that wasn't published
  yet is dropped now, but once that folio is itself published to the instance it
  becomes eligible, so the next publish carries the edge along (Patrick's call,
  2026-06-01). Only edges incident to the batch are scanned, so a publish ships
  what changed plus newly-closable links, not the whole history every time.
  (Spec gap noted: a brand-new edge between two already-published folios that are
  not re-named in any later batch is not picked up — that is "publish an edge",
  a different verb from "publish folios".)

``published`` threads are never sent: they are client-local bookkeeping (which
instances a folio went to), and the instance knows a folio is published simply
by holding it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .station import Station
from .station import UnknownFolio, UnknownSite
from . import wire
from . import sign as _sign


class PublishError(RuntimeError):
    """A publish batch could not be delivered or was rejected by the instance."""


def canonical_instance(url: str) -> str:
    """Canonical identity for a publish target, so cosmetic URL variants collapse.

    The instance identifier is used as both the publish endpoint and the ``to_id``
    of the client's ``published`` bookkeeping edges. If ``http://h:9101`` and
    ``http://h:9101/`` recorded as different strings, convergence eligibility
    (:func:`_already_on_instance`) would miss a folio that IS on the instance and
    silently re-drop its edges, and ``published_instances`` would double-count.
    Canonicalize once — lowercase scheme+host, drop default ports and a trailing
    slash — so one instance is one identity.
    """
    parts = urllib.parse.urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    # Re-bracket an IPv6 literal so the netloc reparses (hostname strips the [ ]).
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((scheme, host, path, "", ""))


def collect_publish_set(
    station: Station,
    refs: Optional[List[str]] = None,
    site: Optional[str] = None,
    instance: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
    """Resolve a selection into (folios, threads, site_slugs) ready for a batch.

    ``site`` publishes every member of the site plus the site folio. ``refs``
    publishes those folios plus, for each, its site folio (so membership lands).
    The two may be combined. ``instance`` is the publish target; when given, an
    edge to a folio already published *there* is eligible to travel too, which is
    what makes dropped edges convergent across successive publishes.

    A folio published by ref carries only its alphabetically-first site
    (``folio_site_slug``); if it belongs to several sites, membership in the
    others lands convergently only when those sites are themselves published.
    """
    folios_by_hash: Dict[str, Dict[str, Any]] = {}
    site_slugs: Dict[str, str] = {}

    def add_folio(row: Dict[str, Any]) -> None:
        folios_by_hash[row["content_hash"]] = row

    def add_site(slug: str) -> None:
        site_hash = station.store.resolve_slug(slug)
        if not site_hash:
            raise UnknownSite(slug)
        site_folio = station.store.get_folio(site_hash)
        if site_folio:
            add_folio(site_folio)
            site_slugs[site_hash] = slug

    if site:
        members = station.folios_in_site(site)  # raises UnknownSite
        add_site(site)
        for m in members:
            add_folio(m)

    for ref in refs or []:
        h = station.resolve_ref(ref)
        if not h:
            raise UnknownFolio(ref)
        folio = station.store.get_folio(h)
        if not folio:
            raise UnknownFolio(ref)
        add_folio(folio)
        # Carry the folio's site so membership reconstructs on the instance.
        slug = station.store.folio_site_slug(h)
        if slug:
            add_site(slug)

    batch = set(folios_by_hash)
    threads = _closed_threads(station, batch, instance)
    return list(folios_by_hash.values()), threads, site_slugs


def _already_on_instance(station: Station, instance: Optional[str]) -> set:
    """Folio hashes already published to ``instance`` (its inbound ``published`` edges)."""
    if not instance:
        return set()
    return {
        t["from_id"]
        for t in station.store.get_threads(
            type=Station.PUBLISHED_THREAD, to_id=instance
        )
        if t.get("from_id")
    }


def _closed_threads(
    station: Station, batch: set, instance: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Edges incident to ``batch`` whose both endpoints are eligible.

    Eligible = in ``batch`` OR already published to ``instance``. Scanning only
    edges incident to the batch keeps a publish proportional to what changed
    while still carrying newly-closable links (an edge whose other end was
    published in an earlier batch). ``published`` bookkeeping edges never travel.
    """
    eligible = batch | _already_on_instance(station, instance)
    seen: Dict[str, Dict[str, Any]] = {}
    for h in batch:
        for edge in station.store.get_threads(from_id=h):
            seen[edge["thread_hash"]] = edge
        for edge in station.store.get_threads(to_id=h):
            seen[edge["thread_hash"]] = edge
    return [
        t
        for t in seen.values()
        if t.get("type") != Station.PUBLISHED_THREAD
        and t.get("from_id") in eligible
        and t.get("to_id") in eligible
    ]


def post_batch(instance_url: str, batch: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    """POST a publish batch to an instance's ingress; return the parsed ack.

    Raises :class:`PublishError` on transport failure or a non-2xx response.
    """
    endpoint = canonical_instance(instance_url) + "/publish/v0/folios"
    body = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise PublishError(f"instance rejected publish ({e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise PublishError(f"could not reach instance at {endpoint}: {e.reason}") from e


def publish(
    station: Station,
    instance_url: str,
    refs: Optional[List[str]] = None,
    site: Optional[str] = None,
    by: Optional[str] = None,
    dry_run: bool = False,
    signer: Optional["_sign.Signer"] = None,
) -> Dict[str, Any]:
    """Collect, push, and (on ack) record publish-state for a selection.

    Returns a result dict: the batch counts, the instance ack, and the folio
    hashes that were marked published locally. On ``dry_run`` nothing is sent
    and nothing is recorded — the collected set is returned for inspection.

    When ``signer`` is given, each folio is signed at this boundary (sign-at-
    publish) and its ``signature_bundle`` rides the wire; the client also keeps a
    copy so its local record reflects what the instance now holds. Without a
    signer the batch crosses unsigned (the pre-mesh / prototype path).
    """
    # Canonicalize the target once so eligibility, recording, and the endpoint
    # all key off one identity (a trailing slash must not fork the instance).
    has_target = not (dry_run and instance_url in (None, "(dry-run)"))
    instance = canonical_instance(instance_url) if has_target else None

    folios, threads, site_slugs = collect_publish_set(
        station, refs=refs, site=site, instance=instance
    )
    if not folios:
        return {"folios": 0, "threads": 0, "dry_run": dry_run, "sent": False}

    batch = wire.build_batch(folios, threads, site_slugs=site_slugs)
    # Sign at the boundary: attach a signature_bundle per folio and keep a
    # client-side copy keyed by content hash to mirror the instance.
    client_bundles: Dict[str, str] = {}
    if signer is not None:
        signed = []
        for wf in batch["folios"]:
            s = _sign.sign_wire_folio(wf, signer)
            client_bundles[s["content_hash"]] = s["signature_bundle"]
            signed.append(s)
        batch["folios"] = signed

    summary: Dict[str, Any] = {
        "instance": instance,
        "folios": len(folios),
        "threads": len(threads),
        "signed": signer is not None,
        "dry_run": dry_run,
        "folio_hashes": [f["content_hash"] for f in folios],
    }
    if dry_run:
        summary["sent"] = False
        return summary

    ack = post_batch(instance, batch)
    summary["ack"] = ack
    summary["sent"] = True
    # Surface refused edges: a "dangling endpoint" rejection means a link the
    # caller expected to close did not — silence here reads as a closed graph
    # when it isn't (the convergence retry only fires on a later publish).
    summary["threads_rejected"] = (ack.get("threads") or {}).get("rejected", [])

    # Record publish-state for the folios the instance accepted or already held.
    landed = set(ack.get("accepted", [])) | set(ack.get("existing", []))
    recorded: List[str] = []
    for f in folios:
        h = f["content_hash"]
        if h in landed:
            station.record_published(h, instance, by=by)
            if h in client_bundles:
                station.store.set_signature(h, client_bundles[h])
            recorded.append(h)
    summary["recorded_published"] = recorded
    return summary
