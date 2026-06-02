"""Instance ingress: the write surface that receives published folios.

PROTOTYPE — unsigned, unauthenticated. This is the counterpart to
``skein_next.publish``: a client POSTs a publish batch here, and the instance
stores the folios and threads, then serves them through its existing read-only
web surface. It is deliberately a SEPARATE surface from the read web app (which
stays read-only) and from any future ``/fed/v0`` peer routes (which are
instance<->instance pull, not client push) — per finding-20260521-kwtb.

What the prototype does and does not do
---------------------------------------
- DOES verify integrity without crypto: every folio and thread is re-hashed from
  its canonical fields and rejected if the claimed hash does not match. A body
  altered in transit cannot keep its address. Storage is idempotent via the
  content hash, so re-publishing is a no-op that reports ``existing``.
- DOES NOT authenticate the writer or verify authorship. There is no signature,
  no account binding, no allowlist. That is exactly the gap signing closes at
  this boundary (brief-20260522-q8k0); the route is structured so that check
  slots in ahead of storage without reshaping the response.

Run it on its own port, against the same data dir the read app serves:

    interskein --data-dir ./instance/.skein-next ingress --port 9101
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .station import Station
from . import wire
from . import sign as _sign

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9101  # read web app is 9001; ingress is a distinct write surface
ENV_DATA_DIR = "SKEIN_NEXT_DATA_DIR"


def _get_data_dir() -> Any:
    return os.environ.get(ENV_DATA_DIR)


class BatchShapeError(ValueError):
    """A publish batch is structurally malformed (bad container types)."""


def _validate_shape(batch: Dict[str, Any]) -> None:
    """Reject a structurally-malformed batch before any storage work.

    Only the container shapes are checked here; per-item integrity (hash match,
    closed endpoints) is handled in the loop. Without this, a body like
    ``{"folios": "x"}`` iterates a string and 500s instead of returning 400.
    """
    for key in ("folios", "threads"):
        value = batch.get(key, [])
        if not isinstance(value, list) or not all(isinstance(i, dict) for i in value):
            raise BatchShapeError(f"{key!r} must be a list of objects")
    if not isinstance(batch.get("site_slugs", {}), dict):
        raise BatchShapeError("'site_slugs' must be an object")


def ingest(
    station: Station,
    batch: Dict[str, Any],
    verifier: "_sign.Verifier" = _sign.default_verifier,
    require_signed: bool = False,
) -> Dict[str, Any]:
    """Store a verified publish batch into ``station``. Returns the ack.

    Pure over the station so it is unit-testable without a server. Folios land
    before threads so a ``within`` edge's site folio (and its slug) already
    exists when membership is recorded.

    A folio carrying a ``signature_bundle`` is verified (bound to its own
    canonical bytes) before storage; a failed signature is rejected and its
    bundle is not stored. ``require_signed`` rejects unsigned folios outright
    (the live-instance posture); it defaults False for the pre-mesh transition,
    where unsigned content still publishes.
    """
    _validate_shape(batch)
    accepted: List[str] = []
    existing: List[str] = []
    rejected: List[Dict[str, str]] = []

    site_slugs: Dict[str, str] = batch.get("site_slugs") or {}

    with station.store.transaction():
        for wf in batch.get("folios", []):
            claimed = wf.get("content_hash")
            reason = wire.folio_reject_reason(wf)
            if reason:
                rejected.append({"content_hash": claimed, "reason": reason})
                continue
            # Signature gate (overlay, separate from the content-hash integrity
            # check above). Unsigned is allowed unless require_signed; a present
            # bundle must verify against this folio's own canonical bytes.
            has_bundle = bool(wf.get("signature_bundle"))
            if has_bundle:
                verified, vreason, _identity = _sign.verify_wire_folio(wf, verifier)
                if not verified:
                    rejected.append({"content_hash": claimed, "reason": f"signature {vreason}"})
                    continue
            elif require_signed:
                rejected.append({"content_hash": claimed, "reason": "unsigned"})
                continue
            if station.store.get_folio(claimed) is not None:
                existing.append(claimed)
            else:
                station.store.create_folio(wf)
                accepted.append(claimed)
            if has_bundle:
                station.store.set_signature(claimed, wf["signature_bundle"])
            # A received site folio carries its human slug alongside the batch.
            if wf.get("type") == "site" and claimed in site_slugs:
                station.store.set_slug(site_slugs[claimed], claimed)

        thread_accepted: List[str] = []
        thread_existing: List[str] = []
        thread_rejected: List[Dict[str, str]] = []

        def present(endpoint: Any) -> bool:
            # On the instance iff it exists in the store — either landed in this
            # batch (visible uncommitted on this connection) or stored by an
            # earlier publish. The latter is what lets convergent edges land:
            # the client ships an A->B edge on B's publish once A already lives
            # here, and this check must see that A.
            return endpoint is not None and station.store.get_folio(endpoint) is not None

        for wt in batch.get("threads", []):
            claimed = wt.get("thread_hash")
            reason = wire.thread_reject_reason(wt)
            if reason:
                thread_rejected.append({"thread_hash": claimed, "reason": reason})
                continue
            # Refuse edges whose endpoints are not both on the instance — the
            # graph stays closed (the client already filters; defense in depth).
            if not present(wt.get("from_id")) or not present(wt.get("to_id")):
                thread_rejected.append(
                    {"thread_hash": claimed, "reason": "dangling endpoint"}
                )
                continue
            already = station.store.get_thread(claimed) is not None
            station.store.save_thread(
                from_id=wt.get("from_id"),
                to_id=wt.get("to_id"),
                type=wt.get("type"),
                weaver=wt.get("weaver"),
                created_at=wt.get("created_at"),
                content=wt.get("content"),
            )
            (thread_existing if already else thread_accepted).append(claimed)

    return {
        "protocol": wire.PROTOCOL,
        "accepted": accepted,
        "existing": existing,
        "rejected": rejected,
        "threads": {
            "accepted": thread_accepted,
            "existing": thread_existing,
            "rejected": thread_rejected,
        },
    }


ENV_REQUIRE_SIGNED = "SKEIN_NEXT_REQUIRE_SIGNED"


def _require_signed() -> bool:
    """Whether this instance rejects unsigned publishes (off unless env opts in).

    The signature gate is a per-instance posture, not a hardcode: a public
    instance can demand signed content by setting the env, while the pre-mesh /
    local default stays open. (Full enforcement is the auth phase — this just
    keeps the knob reachable instead of dead.)
    """
    return os.environ.get(ENV_REQUIRE_SIGNED, "").strip().lower() in ("1", "true", "yes")


def create_app() -> FastAPI:
    app = FastAPI(
        title="SKEIN (next) — ingress",
        description="client->instance publish ingress",
        version="0.0.1",
    )
    require_signed = _require_signed()

    @app.post("/publish/v0/folios")
    def publish_folios(batch: Dict[str, Any]) -> JSONResponse:
        if batch.get("protocol") != wire.PROTOCOL:
            return JSONResponse(
                status_code=400,
                content={"error": f"unknown protocol {batch.get('protocol')!r}; "
                                  f"this instance speaks {wire.PROTOCOL!r}"},
            )
        # Open a write station per request (own SQLite connection, threadpool-safe).
        station = Station(_get_data_dir(), check_same_thread=False)
        try:
            ack = ingest(station, batch, require_signed=require_signed)
        except BatchShapeError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        finally:
            station.close()
        return JSONResponse(status_code=200, content=ack)

    return app


def run_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    app = create_app()
    logger.info("Starting new-skein ingress on http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
