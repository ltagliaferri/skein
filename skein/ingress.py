"""Instance ingress: the write surface that receives published folios.

The counterpart to ``skein.publish``: a client POSTs a publish batch here, and the
station verifies + stores the folios and threads, then serves them through the
read-only web surface. It is deliberately a SEPARATE surface from the read web app
(which stays read-only) and from any future ``/fed/v0`` peer routes (which are
instance<->instance pull, not client push) — per finding-20260521-kwtb.

Re-homed from ``skein_next/ingress.py`` (station re-home Stage 3) onto the Stage-1
``StationStore`` + Stage-2 verify libs; behavior preserved on the wire, including
the meql/Phase-4 hardening (body byte-caps, quiet ClientDisconnect, RecursionError
-> 400, write-lock -> retryable 503) and the require_signed manifest/binding gate.

What the surface does and does not do
-------------------------------------
- DOES verify integrity: every folio and thread is re-hashed from its canonical
  fields and rejected if the claimed hash does not match. A body altered in transit
  cannot keep its address. Storage is idempotent via the content hash, so
  re-publishing is a no-op that reports ``existing``.
- Under ``require_signed`` it ALSO gates admission on a verified, bound manifest
  signer (the unified constituent table); under the default OFF posture it is
  manifest-blind and binding-blind, byte-identical to the pre-mesh posture.

Run it on its own port, against the same data dir the read app serves (the
``skein.station`` launcher arrives in Stage 6):

    python -m skein.ingress   # DEFAULT_PORT 9101; read web app is 9001
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .station import Station, StationBootError
from . import canon, wire
from . import sign as _sign
from . import redeem as _redeem
from .authorization import Principal, default_bindings
from .identity import content_hash_for_bytes
from .thread_authz import (
    AuthzReject,
    LineageReject,
    authorize_thread,
    authorized_staged_supersedes,
    owns_folio,
)
from .station_env import StationEnvError, station_env
from .station_store import DB_FILENAME, bundle_hash_for, sqlite_error_is_lock

logger = logging.getLogger(__name__)

# The bare manifest-failure reasons the verifier returns are NOT wrapped; only a
# genuine VerifyStatus crypto failure wears the 'manifest signature ' prefix
# (fell-r1 FIX 1, the reject-reason propagation rule). These are the absence +
# wire-integrity bare reasons.
_BARE_MANIFEST_REASONS = frozenset(
    {"no manifest", "manifest malformed", "wrong kind", "unknown profile"}
)


def _constituent_manifest_reject_reason(m_reason: str) -> str:
    """Map a verifier manifest reason to the constituent reject reason (3 buckets).

    ABSENCE / WIRE-INTEGRITY propagate the BARE reason; the CRYPTO FLOOR (a genuine
    VerifyStatus value) is the ONLY bucket wrapped 'manifest signature <status>'."""
    if m_reason in _BARE_MANIFEST_REASONS:
        return m_reason
    return f"manifest signature {m_reason}"

DEFAULT_PORT = 9101  # read web app is 9001; ingress is a distinct write surface
# Env reads resolve through station_env: the SKEIN_STATION_* canonical name.
ENV_DATA_DIR = "SKEIN_STATION_DATA_DIR"

# The station's canonical origin — a SIGNED field of the redeem challenge (INV-1).
# The collaborator signs over this exact string; the station reconstructs the
# challenge with its OWN configured value, so a proof minted for a different origin
# fails closed. Set it in the deploy env (e.g. https://interskein.com). If unset,
# the redeem route refuses to operate (it cannot verify a token-bound proof without
# an authoritative origin) — /publish is unaffected.
ENV_ORIGIN = "SKEIN_STATION_ORIGIN"

# Body cap for the redeem route — SMALLER than the publish cap: a redeem body is a
# single {token, proof} object (a token string + one Sigstore bundle), never a
# multi-folio batch. 64 KiB is generous for one bundle yet bounds parse/memory work
# hard. Its own nginx location carries a matching client_max_body_size + a tighter
# per-IP limit_req zone (deploy/nginx).
REDEEM_MAX_BYTES = 64 * 1024

# The publish + redeem routes both degrade a transient write-lock to a retryable
# 503; the discrimination lives in station_store.sqlite_error_is_lock — one source
# of truth.
_sqlite_error_is_lock = sqlite_error_is_lock

# Absolute byte cap on a publish request body, enforced BEFORE the body is fully
# buffered or JSON-parsed, so a hostile client cannot force unbounded parse/memory
# work ahead of any verification. Matches the public route's nginx
# client_max_body_size (1 MiB); the app cap is defense-in-depth for any path that
# reaches the ingress without the fronting proxy (e.g. a loopback SSH tunnel). If
# a legitimate publish ever needs more, bump BOTH this and the nginx cap together.
MAX_BATCH_BYTES = 1024 * 1024


def _get_data_dir() -> Any:
    return station_env("DATA_DIR")


class BatchShapeError(ValueError):
    """A publish batch is structurally malformed (bad container types)."""


def _validate_shape(batch: Dict[str, Any]) -> None:
    """Reject a structurally-malformed batch before any storage work.

    Only the container shapes are checked here; per-item integrity (hash match,
    closed endpoints) and the manifest's semantic consistency are handled in the
    loop / the verifier. Without this, a body like ``{"folios": "x"}`` iterates a
    string and 500s instead of returning 400.

    The ``manifest_signature`` is DELIBERATELY not shape-checked here. A present-
    but-non-dict value (``None``, a string, a list, an int) is NOT a batch 400: it
    flows to ``verify_wire_manifest`` (TOTAL over hostile input) and becomes the
    per-constituent WIRE-INTEGRITY verdict 'manifest malformed' (VM11/VM12). Only a
    WHOLLY ABSENT key is the ABSENCE bucket ('no manifest'). The ``mctx`` dict-
    indexing downstream is gated on ``m_verified`` (False for any non-dict), so a
    non-dict manifest never reaches dict-indexing and never 500s.
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
    bindings: Any = None,
) -> Dict[str, Any]:
    """Store a publish batch into ``station`` under the unified manifest gate.

    Pure over the station so it is unit-testable without a server. Folios land
    before threads so a ``within`` edge's site folio (and its slug) already exists
    when membership is recorded.

    The manifest verdict is computed ONCE before the loops: the batch's single
    ``manifest_signature`` is verified and (under ``require_signed``) its signer is
    gated through the bindings exactly ONCE. Then every constituent — folio OR
    thread — is judged identically by MEMBERSHIP (its content hash is a leaf under
    the verified bound manifest).

    - ``require_signed=False`` (OFF) is manifest-blind AND binding-blind: a
      constituent admits on integrity + closed-graph alone, byte-identical to the
      pre-mesh posture. No bindings are consulted, no attribution is written.
    - ``require_signed=True`` (ON): a constituent admits iff it is a leaf under a
      manifest that verifies and whose signer is bound + non-revoked. On admit
      (whether the body is freshly stored or already 'existing'), the manifest +
      constituent attribution are written (INSERT OR IGNORE).

    ``bindings`` defaults to a BindingStore over ``station.store``."""
    _validate_shape(batch)
    if bindings is None:
        bindings = default_bindings(station.store)

    # --- compute the manifest verdict ONCE (before the per-item loops) --------
    has_manifest = "manifest_signature" in batch  # KEY-PRESENCE, not bool()
    m_verified, m_reason, m_identity = (False, "no manifest", None)
    # OFF is manifest-blind (RS1/RS9): never call verify_wire_manifest — under OFF
    # a real Sigstore verification (network/TUF, and an abort if the verifier
    # raises) is NOT byte-identical to the pre-mesh posture. The verdict is only
    # computed under ON, where the loops consult manifest_decision/attribution; the
    # verify call fires exactly ONCE here. We keep has_manifest (key-presence) on
    # the ON path so a present-but-non-dict value still reaches the verifier as the
    # WIRE-INTEGRITY 'manifest malformed' verdict (VM11), distinct from 'no manifest'.
    if has_manifest and require_signed:
        m_verified, m_reason, m_identity = _sign.verify_wire_manifest(
            batch["manifest_signature"], verifier
        )

    # The verified manifest signer's binding is read ONCE for the whole batch (RS8),
    # LIVE inside the ingest BEGIN IMMEDIATE (below), never here pre-transaction: the
    # single get_binding is the re-gating pin C40 / §8, so a revocation that commits
    # before the write lock flips the verdict on THIS ingest, not the next. These are
    # initialized here only so manifest_decision (a closure over them) has values to
    # read; the authoritative assignment happens live in the transaction.
    m_bound = False
    m_bind_reason: Optional[str] = None

    # Manifest context for the attribution writes (only meaningful when verified).
    mctx: Optional[Dict[str, Any]] = None
    if has_manifest and m_verified:
        ms = batch["manifest_signature"]
        descriptor = ms["descriptor"]
        root = descriptor["root"]
        leaf_count = descriptor["leaf_count"]
        mctx = {
            "root": root,
            "leaf_count": leaf_count,
            "leaf_list": ms["leaf_list"],
            "descriptor_json": json.dumps(descriptor, sort_keys=True),
            "leaf_list_json": json.dumps(ms["leaf_list"]),
            "bundle_json": ms["signature_bundle"],
            "manifest_hash": content_hash_for_bytes(
                canon.manifest_descriptor_canonical_bytes(root, leaf_count)
            ),
            "issuer": m_identity["issuer"],
            "subject": m_identity["subject"],
        }

    def manifest_decision(constituent_hash: str) -> Tuple[bool, Optional[str]]:
        """Under ON, whether this constituent is admitted, and the reject reason."""
        if not m_verified:
            return False, _constituent_manifest_reject_reason(m_reason)
        if not m_bound:
            return False, m_bind_reason
        if not canon.manifest_membership(mctx["leaf_list"], mctx["root"], constituent_hash):
            return False, "not in manifest"
        return True, None

    def write_attribution(constituent_hash: str, kind: str) -> None:
        """Record manifest + attribution (manifest row FIRST, ST8), INSERT OR
        IGNORE — runs on BOTH the accept AND the 'existing' path (RS7/RS20). Also
        populates the manifest's VERIFIED signature verdict in verify_cache (the
        ingress is the cache WRITER, VC6); the read path elides Sigstore on a hit."""
        station.store.add_manifest(
            mctx["root"], mctx["manifest_hash"], mctx["descriptor_json"],
            mctx["leaf_list_json"], mctx["bundle_json"], mctx["issuer"],
            mctx["subject"], mctx["leaf_count"],
        )
        station.store.add_constituent_attribution(
            constituent_hash, kind, mctx["root"], mctx["issuer"], mctx["subject"]
        )
        station.store.verify_cache_put(
            mctx["manifest_hash"], bundle_hash_for(mctx["bundle_json"]),
            "VERIFIED", mctx["issuer"], mctx["subject"],
        )

    accepted: List[str] = []
    existing: List[str] = []
    rejected: List[Dict[str, str]] = []

    site_slugs: Dict[str, str] = batch.get("site_slugs") or {}

    with station.store.transaction():
        # RE-READ the signer's binding LIVE inside BEGIN IMMEDIATE (§8, C40): the
        # pre-transaction m_bound (ingress.py, computed at :191) is a PRE-FILTER only.
        # A revocation that commits between that read and this lock must flip the
        # verdict, so a revoked signer's tiered writes are never admitted in the
        # window. Reassigning m_bound/m_bind_reason here updates what manifest_decision
        # (a closure over them) sees at call time. `signer` is the Principal the thread
        # authorization gates on; it is set ONLY when the live binding is active.
        signer: Optional[Principal] = None
        live = None
        if require_signed and has_manifest and m_verified:
            live = bindings.get_binding(m_identity["issuer"], m_identity["subject"])
            if live is None:
                m_bound, m_bind_reason = False, "unbound signer"
            elif live.revoked_at is not None:
                m_bound, m_bind_reason = False, "revoked binding"
            else:
                m_bound, m_bind_reason = True, None
                signer = Principal(m_identity["issuer"], m_identity["subject"])
        # Whether the live signer holds a tier that may OVERRIDE a slug collision
        # (an administrator/operator re-point, §6). Read from the same live binding.
        slug_override = bool(
            require_signed
            and signer is not None
            and live is not None
            and live.role in ("operator", "administrator")
        )

        for wf in batch.get("folios", []):
            claimed = wf.get("content_hash")
            reason = wire.folio_reject_reason(wf)
            if reason:
                rejected.append({"content_hash": claimed, "reason": reason})
                continue
            if require_signed:
                admit, mreason = manifest_decision(claimed)
                if not admit:
                    rejected.append({"content_hash": claimed, "reason": mreason})
                    continue
            try:
                with station.store.savepoint():
                    newly_created = station.store.get_folio(claimed) is None
                    if newly_created:
                        station.store.create_folio(wf)
                        accepted.append(claimed)
                    else:
                        existing.append(claimed)
                    # Attribute a folio to the signer ONLY when the signer CREATED it in
                    # this batch, or ALREADY owns it (re-affirm). NEVER first-attribute a
                    # PRE-EXISTING previously-unattributed (legacy) folio to a re-publisher:
                    # that would let a bound non-owner CAPTURE a legacy owner-None lineage
                    # (fell-r1 finding 1) and then supersede it — reopening the exact hole
                    # this model closes. A legacy/unsigned folio stays owner-None, actionable
                    # only by a station tier (§4).
                    if require_signed and signer is not None and (
                        newly_created or owns_folio(station.store, signer, claimed)
                    ):
                        write_attribution(claimed, "folio")
                    # A site folio's slug is gated TRANSITIVELY: this claim is only
                    # reached for an admitted site folio (Q2/RS18). Under ON the claim is
                    # keyed on the signer PAIR (§3.3/§6): free-or-same-signer re-anchors,
                    # a DIFFERENT signer COLLIDES (the existing claim stands; the folio is
                    # still stored, just not renamed) unless an administrator/operator
                    # overrides. Signer-blind OFF keeps the last-write-wins set_slug.
                    if wf.get("type") == "site" and claimed in site_slugs:
                        if require_signed and signer is not None:
                            station.store.claim_slug(
                                site_slugs[claimed], claimed,
                                signer.issuer, signer.subject, override=slug_override,
                            )
                        else:
                            station.store.set_slug(site_slugs[claimed], claimed)
            except Exception as exc:  # noqa: BLE001 — one bad item never 500s the batch
                logger.debug("folio %s rolled back: %s", claimed, exc)
                if claimed in accepted:
                    accepted.remove(claimed)
                if claimed in existing:
                    existing.remove(claimed)
                rejected.append({"content_hash": claimed, "reason": "invalid fields"})

        thread_accepted: List[str] = []
        thread_existing: List[str] = []
        thread_rejected: List[Dict[str, str]] = []
        thread_dangling: List[str] = []

        # The STAGED FINAL GRAPH (§8): the fixpoint of AUTHORIZED same-batch supersedes.
        # Built AFTER the folio loop (so same-batch new-version folios and their
        # attributions are landed and ownership resolves) and BEFORE the thread loop (so
        # every affecting edge's genesis resolves over the batch's pending supersedes).
        # A rejected supersedes never enters it (knuth F2). Only membership-valid signed
        # supersedes are candidates.
        staged_supersedes: List[Tuple[str, str]] = []
        if require_signed and m_verified and m_bound and signer is not None:
            batch_supersedes = []
            for wt in batch.get("threads", []):
                if wt.get("type") != "supersedes":
                    continue
                if wire.thread_reject_reason(wt):
                    continue
                if manifest_decision(wt.get("thread_hash"))[0]:
                    batch_supersedes.append(wt)
            staged_supersedes = authorized_staged_supersedes(
                station.store, signer, batch_supersedes
            )

        def present(endpoint: Any) -> bool:
            # On the instance iff it exists in the store — either landed in this
            # batch (visible uncommitted on this connection) or stored by an
            # earlier publish. The latter is what lets convergent edges land.
            return endpoint is not None and station.store.get_folio(endpoint) is not None

        for wt in batch.get("threads", []):
            claimed = wt.get("thread_hash")
            reason = wire.thread_reject_reason(wt)
            if reason:
                thread_rejected.append({"thread_hash": claimed, "reason": reason})
                continue
            # Under ON, a GLOBAL manifest failure (unverified, or a verified signer
            # that is unbound/revoked) rejects EVERY constituent identically with the
            # manifest/bind reason, BEFORE the per-thread present() check — so a
            # broken manifest makes folios AND threads report the same reason (the
            # unified-table principle; VM11). manifest_decision returns exactly that
            # reason when not verified / not bound, so we reuse it here.
            if require_signed and (not m_verified or not m_bound):
                _, mreason = manifest_decision(claimed)
                thread_rejected.append({"thread_hash": claimed, "reason": mreason})
                continue
            # Dangling endpoints are ALLOWED (accept-and-flag, PHASE_4_DESIGN §5.3): a
            # member-thread whose endpoint folio has not (yet) landed is STORED and
            # NOTED, never rejected — its hash is a signed manifest leaf regardless of
            # whether its endpoints are held, and the threads table has no endpoint FK.
            # The manifest_decision gate below still applies, so under require_signed a
            # dangling edge must still be a signed member.
            dangling = not present(wt.get("from_id")) or not present(wt.get("to_id"))
            if require_signed:
                # Manifest verified + bound, so this can only fail 'not in manifest'.
                admit, mreason = manifest_decision(claimed)
                if not admit:
                    thread_rejected.append({"thread_hash": claimed, "reason": mreason})
                    continue
                # BY-ENDS AUTHORIZATION (§5.1): a signed manifest leaf is necessary but
                # not sufficient — the signer must be authorized to ORIGINATE this typed
                # edge given its endpoints. Resolved over the staged final graph, with
                # tier + grants read LIVE (inside this BEGIN IMMEDIATE). Fail-closed:
                # AuthzReject/LineageReject reject this one edge with its wire reason,
                # never a 500 and never the sibling edges.
                try:
                    authorize_thread(station.store, signer, wt, staged_supersedes)
                except (AuthzReject, LineageReject) as exc:
                    thread_rejected.append(
                        {"thread_hash": claimed,
                         "reason": getattr(exc, "reason", str(exc))}
                    )
                    continue
            try:
                with station.store.savepoint():
                    already = station.store.get_thread(claimed) is not None
                    station.store.save_thread(
                        from_id=wt.get("from_id"),
                        to_id=wt.get("to_id"),
                        type=wt.get("type"),
                        weaver=wt.get("weaver"),
                        created_at=wt.get("created_at"),
                        content=wt.get("content"),
                    )
                    # save_thread is INSERT OR IGNORE: if the row is NOT present after it
                    # AND was not already there, the insert was silently swallowed by a
                    # constraint — the <=1-parent partial-unique index (a second supersedes
                    # parent for this from_id, i.e. a MERGE). Report it HONESTLY as rejected
                    # rather than a false 'accepted' — the ack must never claim a row it did
                    # not persist (fell-r1 finding 3; the OFF-posture backstop, since under
                    # ON authorize_thread already rejects a merge before save).
                    if not already and station.store.get_thread(claimed) is None:
                        thread_rejected.append(
                            {"thread_hash": claimed,
                             "reason": "supersedes would create a second parent (merge)"}
                        )
                    else:
                        if require_signed:
                            write_attribution(claimed, "thread")
                        (thread_existing if already else thread_accepted).append(claimed)
                        if dangling:
                            thread_dangling.append(claimed)
            except Exception as exc:  # noqa: BLE001 — one bad item never 500s the batch
                logger.debug("thread %s rolled back: %s", claimed, exc)
                for lst in (thread_accepted, thread_existing):
                    if claimed in lst:
                        lst.remove(claimed)
                thread_rejected.append({"thread_hash": claimed, "reason": "invalid fields"})

    return {
        "protocol": wire.PROTOCOL,
        "accepted": accepted,
        "existing": existing,
        "rejected": rejected,
        "threads": {
            "accepted": thread_accepted,
            "existing": thread_existing,
            "rejected": thread_rejected,
            "dangling": thread_dangling,
        },
    }


def backfill_verify_cache(station: Station, verifier: "_sign.Verifier" = None) -> int:
    """Populate verify_cache with the SIGNATURE verdict of every stored manifest.

    The maintenance verb (run when the ingress is quiesced): over a corpus of
    signed manifests it writes one stable verdict row per manifest, recomputing the
    Sigstore verdict via verify_wire_manifest. Idempotent — a second run re-writes
    the same rows (VC9). Returns the number of manifests processed."""
    if verifier is None:
        verifier = _sign.default_verifier
    count = 0
    for m in station.store.all_manifests():
        manifest_signature = {
            "descriptor": json.loads(m["descriptor_json"]),
            "leaf_list": json.loads(m["leaf_list_json"]),
            "signature_bundle": m["bundle_json"],
        }
        verified, reason, identity = _sign.verify_wire_manifest(manifest_signature, verifier)
        status = "VERIFIED" if verified else reason
        iss = (identity or {}).get("issuer") if identity else m["issuer"]
        sub = (identity or {}).get("subject") if identity else m["subject"]
        station.store.verify_cache_put(
            m["manifest_hash"], bundle_hash_for(m["bundle_json"]), status, iss, sub
        )
        count += 1
    return count


ENV_REQUIRE_SIGNED = "SKEIN_STATION_REQUIRE_SIGNED"

_REQUIRE_SIGNED_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled", "y"})
_REQUIRE_SIGNED_FALSY = frozenset({"0", "false", "no", "off", ""})


class RequireSignedConfigError(RuntimeError):
    """SKEIN_STATION_REQUIRE_SIGNED is set to a value that is neither a recognized
    truthy nor falsy spelling. A security toggle must fail LOUD on a typo, never
    silently pick the open posture."""


def _require_signed() -> bool:
    """Whether this instance rejects unsigned publishes (off unless env opts in).

    The signature gate is a per-instance posture, not a hardcode: a public
    instance can demand signed content by setting the env, while the pre-mesh /
    local default stays open. (Full enforcement is the auth phase — this just
    keeps the knob reachable instead of dead.)

    An UNSET env is the falsy default. A SET-but-unrecognized value (e.g. a typo
    like ``TRUE!`` or an unsupported spelling) is refused with a raised
    ``RequireSignedConfigError`` rather than silently treated as off — the
    alternative lets a plausible-looking misconfiguration boot the public
    ingress wide open with only a log line nobody reads.
    """
    value = (station_env("REQUIRE_SIGNED") or "").strip().lower()
    if value in _REQUIRE_SIGNED_TRUTHY:
        return True
    if value in _REQUIRE_SIGNED_FALSY:
        return False
    raise RequireSignedConfigError(
        f"{ENV_REQUIRE_SIGNED}={value!r} is not a recognized boolean spelling "
        f"(truthy: {sorted(_REQUIRE_SIGNED_TRUTHY)}, falsy: {sorted(_REQUIRE_SIGNED_FALSY)}); "
        "refusing to boot rather than silently choosing a posture"
    )


class OperatorInvariantError(RuntimeError):
    """The ingress startup invariant: under require_signed the active-operator count
    MUST be exactly 1 (D13/D20). Refuses boot LOUD rather than running misconfigured."""


def create_app() -> FastAPI:
    require_signed = _require_signed()
    # Resolve the data dir ONCE at boot in EITHER posture (deep_code_audit, fell
    # r4): a conflicting new/legacy SKEIN_*_DATA_DIR pair must refuse startup
    # here, not surface as a StationEnvError 500 on the first request — under
    # require_signed=0 the only pre-fix read was per-request.
    boot_data_dir = _get_data_dir()
    # BOOT I/O TOTALITY (brief-20260712-t1tf #4): open the corpus ONCE here, in
    # EITHER posture, so an unusable db refuses startup as a clean typed error —
    # never the raw sqlite3 traceback the require_signed operator count used to
    # leak, and never (under OFF) a 500 on the first publish. A MISSING db is NOT
    # a fault on this write surface: the read_write open legitimately births it,
    # exactly as the first request's per-request open always has. The fault class
    # is corrupt/unopenable — garbage bytes at the db path, the db path a
    # directory, the data dir itself a file (OSError from mkdir), or a
    # non-station corpus at the path (StationStore's ValueError refusals).
    if not boot_data_dir:
        raise StationBootError(
            f"no station data dir is configured ({ENV_DATA_DIR} is unset); "
            "the ingress has no corpus to open"
        )
    boot_db_path = Path(boot_data_dir) / DB_FILENAME
    try:
        station = Station(boot_data_dir, check_same_thread=False)
    except (sqlite3.Error, OSError, ValueError) as e:
        raise StationBootError(
            f"station corpus at {boot_db_path} is unopenable: {e}"
        ) from e
    # STARTUP INVARIANT (the ingress only — the read app is EXEMPT, D17): under
    # require_signed the active operator count must be EXACTLY 1. Count 0 or >1
    # refuses boot. The operator is read from the account_bindings sidecar, the
    # single source of truth (D16), never a stationfile field.
    try:
        n_ops = station.store.count_active_operators() if require_signed else None
    except sqlite3.Error as e:
        # The open resolved but the first real read didn't (e.g. an I/O error,
        # or a truncation whose fault only surfaces on page reads).
        raise StationBootError(
            f"station corpus at {boot_db_path} is unreadable: {e}"
        ) from e
    finally:
        station.close()
    if require_signed:
        if n_ops != 1:
            if n_ops == 0:
                raise OperatorInvariantError(
                    "require_signed is on but no active operator exists; "
                    "run 'skein station account init-operator' before starting the ingress"
                )
            raise OperatorInvariantError(
                f"require_signed is on but {n_ops} active operators exist; the "
                "single-active-operator invariant is violated — resolve with "
                "'skein station account rotate-operator' / 'revoke' before starting"
            )
        logger.info("ingress starting with require_signed: 1 active operator present")
    else:
        logger.warning(
            "ingress starting with require_signed OFF — unsigned content is accepted"
        )

    # The station's authoritative origin for the redeem token-binding (INV-1). Read
    # ONCE at app creation. CANONICALIZED with the same normalizer the client uses
    # on its --to value (publish.canonical_instance: lowercase scheme+host, drop
    # default ports and trailing slash) so the station reconstructs the EXACT string
    # the collaborator signed over. Without this a trailing slash / uppercase scheme
    # / explicit :443 in SKEIN_STATION_ORIGIN would diverge from the client's
    # canonicalized origin and SIGNATURE_MISMATCH every redeem (fail-closed, but an
    # availability footgun). If unset, the redeem route refuses to operate; /publish
    # is unaffected.
    from .publish import canonical_instance as _canonical_instance

    _raw_origin = station_env("ORIGIN")
    # Totality (finding-20260709-p4n5 #1): a mistyped origin (bad port, unclosed
    # IPv6 bracket) must be a clean config refusal at startup, not a raw
    # ValueError traceback out of urllib.
    try:
        redeem_origin = _canonical_instance(_raw_origin) if _raw_origin else None
    except ValueError as e:
        raise StationEnvError(
            f"{ENV_ORIGIN} is set to {_raw_origin!r}, which is not a valid origin "
            f"URL ({e}); expected e.g. https://ingress.interskein.com"
        ) from e
    if redeem_origin:
        logger.info("ingress redeem origin: %s", redeem_origin)
    else:
        logger.warning(
            "ingress starting without %s — /invite/redeem will refuse to operate "
            "until the station origin is configured", ENV_ORIGIN
        )

    app = FastAPI(
        title="SKEIN — station ingress",
        description="client->instance publish ingress",
        version="0.0.1",
    )

    @app.post("/publish/v0/folios")
    async def publish_folios(request: Request) -> JSONResponse:
        # Bound the body by BYTES before buffering / parsing (content-length fast-
        # path + a streaming guard), so a hostile client cannot force unbounded
        # parse/memory work before any verification runs. Over the cap is rejected
        # WHOLE (413), never truncated. Mirrors the read app's resolve batch cap and
        # is defense-in-depth atop the fronting proxy's client_max_body_size.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_BATCH_BYTES:
                    return JSONResponse(status_code=413, content={"error": "request body too large"})
            except ValueError:
                pass
        body = bytearray()
        try:
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_BATCH_BYTES:
                    return JSONResponse(status_code=413, content={"error": "request body too large"})
        except ClientDisconnect:
            # The client went away mid-body (a slow-loris that bails, a flaky
            # network, or a deliberate probe). A disconnect is normal and fully
            # adversary-controllable, so it must be handled QUIETLY — not raised
            # as an ASGI application error. Left uncaught it logs a full traceback
            # per abandoned request, which at flood rate is a cheap log-
            # amplification DoS (disk fill + real-error burial) and breaks this
            # module's 'never 500s over hostile input' contract. The socket is
            # gone, so this response is discarded; we just stop cleanly.
            return JSONResponse(status_code=400, content={"error": "client disconnected"})

        try:
            batch = json.loads(body) if body else None
        except (ValueError, RecursionError):
            # ValueError covers malformed JSON and the UnicodeDecodeError/int-string
            # limit subclasses. RecursionError is the OTHER thing json.loads can raise
            # on hostile input: deeply-nested arrays/objects ([[[... ) blow Python's
            # recursion limit. It is NOT a ValueError (it's a RuntimeError), so left
            # uncaught it escapes to a 500 + full ASGI traceback per request — a cheap
            # floodable log-amplification DoS (the 1 MiB body cap bounds BYTES, not
            # nesting depth: '[' is one byte per level, so ~hundreds of thousands of
            # levels fit under the cap). Reject it as the malformed JSON it is.
            return JSONResponse(status_code=400, content={"error": "request body is not valid JSON"})
        if not isinstance(batch, dict):
            return JSONResponse(status_code=400, content={"error": "request body must be a JSON object"})
        if batch.get("protocol") != wire.PROTOCOL:
            return JSONResponse(
                status_code=400,
                content={"error": f"unknown protocol {batch.get('protocol')!r}; "
                                  f"this instance speaks {wire.PROTOCOL!r}"},
            )

        # ingest does blocking SQLite work; the route is async (to stream-bound the
        # body), so run the open->ingest->close off the event loop the way the sync
        # routes get the threadpool — a slow write never stalls body reads on other
        # requests. The station (own SQLite connection) is created and closed inside
        # the worker thread so it never crosses threads.
        def _do_ingest() -> Any:
            station = Station(_get_data_dir(), check_same_thread=False)
            try:
                return ingest(station, batch, require_signed=require_signed)
            finally:
                station.close()

        try:
            ack = await run_in_threadpool(_do_ingest)
        except BatchShapeError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        except sqlite3.OperationalError as e:
            # Write-lock contention beyond busy_timeout: BEGIN IMMEDIATE could not
            # take the lock in time and raised on ENTRY to transaction(), before the
            # per-item handlers, so it surfaces here. Degrade GRACEFULLY — a 503 the
            # client can retry — instead of letting it propagate to an uncaught 500
            # + full ASGI traceback per request (the log-amplification DoS this same
            # change set hardens against). A genuine (non-lock) OperationalError is a
            # real fault: re-raise it so it is not masked as transient. The lock
            # discrimination (numeric result-code, extended-code masked to the
            # primary byte, message-text fallback) is shared with the redeem route.
            if not _sqlite_error_is_lock(e):
                raise
            logger.warning("ingress write-lock contention; returning 503: %s", e)
            return JSONResponse(
                status_code=503,
                content={"error": "instance busy, retry shortly"},
                headers={"Retry-After": "1"},
            )
        return JSONResponse(status_code=200, content=ack)

    @app.post(_sign.REDEEM_ROUTE)
    async def invite_redeem(request: Request) -> JSONResponse:
        # A SECOND hand-written public Sigstore-doing route. It REPLICATES the
        # /publish hardening shell line-for-line — it does NOT inherit it (INV-5):
        # a SMALLER body cap (the body is one {token, proof}), content-length
        # fast-path + streaming guard, quiet ClientDisconnect, JSON-parse (incl
        # RecursionError) -> 400, blocking work off the event loop with the Station
        # opened+closed inside the worker, and SQLite-lock -> retryable 503.
        if not redeem_origin:
            # Misconfiguration, not hostile input: without an authoritative origin a
            # token-bound proof cannot be reconstructed, so refuse rather than verify
            # against a wrong/empty origin. 503 (operator-fixable), not a 500.
            return JSONResponse(
                status_code=503,
                content={"error": "redeem is not configured on this instance"},
            )

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > REDEEM_MAX_BYTES:
                    return JSONResponse(status_code=413, content={"error": "request body too large"})
            except ValueError:
                pass
        body = bytearray()
        try:
            async for chunk in request.stream():
                body += chunk
                if len(body) > REDEEM_MAX_BYTES:
                    return JSONResponse(status_code=413, content={"error": "request body too large"})
        except ClientDisconnect:
            # Adversary-controllable disconnect handled QUIETLY (no ASGI traceback /
            # log-amplification), exactly as /publish does.
            return JSONResponse(status_code=400, content={"error": "client disconnected"})

        try:
            payload = json.loads(body) if body else None
        except (ValueError, RecursionError):
            # RecursionError (deeply-nested JSON) is a RuntimeError, not a ValueError;
            # catching it here is the same just-merged /publish fix — left uncaught it
            # is a floodable 500 + traceback per request.
            return JSONResponse(status_code=400, content={"error": "request body is not valid JSON"})
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"error": "request body must be a JSON object"})
        token = payload.get("token")
        proof = payload.get("proof")
        if not isinstance(token, str) or not token:
            return JSONResponse(status_code=400, content={"error": "missing or invalid 'token'"})

        # The redeem orchestration does the cheap checks, the crypto (OUTSIDE any
        # lock), and the short CAS burn+bind. Run it off the event loop with the
        # Station opened + closed inside the worker thread so its SQLite connection
        # never crosses threads (the /publish threadpool discipline).
        def _do_redeem() -> "_redeem.RedeemResult":
            station = Station(_get_data_dir(), check_same_thread=False)
            try:
                return _redeem.redeem(station, token, proof, redeem_origin)
            finally:
                station.close()

        try:
            result = await run_in_threadpool(_do_redeem)
        except sqlite3.OperationalError as e:
            # The burn transaction (BEGIN IMMEDIATE in redeem_invite_cas) could not
            # take the write lock in time. Degrade to a retryable 503; a genuine
            # (non-lock) fault re-raises. verify_multi runs OUTSIDE the lock, so a
            # slow Sigstore round-trip never produces this (INV-2).
            if not _sqlite_error_is_lock(e):
                raise
            logger.warning("redeem write-lock contention; returning 503: %s", e)
            return JSONResponse(
                status_code=503,
                content={"error": "instance busy, retry shortly"},
                headers={"Retry-After": "1"},
            )

        http_status = _redeem.HTTP_STATUS.get(result.status, 400)
        content: Dict[str, Any] = {"ok": result.ok, "status": result.status}
        if result.ok:
            content["issuer"] = result.issuer
            content["subject"] = result.subject
        else:
            content["error"] = result.reason
        return JSONResponse(status_code=http_status, content=content)

    return app


def run_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    app = create_app()
    logger.info("Starting skein station ingress on http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    """``python -m skein.ingress`` — the direct-entry twin of the ``skein
    station ingress`` launcher's ClickException (fell r4, codex): a misconfigured
    env — or an unusable corpus db (StationBootError) — exits with a clean
    one-line message, never a raw traceback."""
    import sys

    try:
        run_server()
    except (
        StationEnvError, RequireSignedConfigError, OperatorInvariantError, StationBootError,
    ) as e:
        print(f"ingress will not start: {e}", file=sys.stderr)
        raise SystemExit(2) from e


if __name__ == "__main__":
    main()
