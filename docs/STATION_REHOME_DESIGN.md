# Station re-home design gate — retire `skein_next`, re-home its live station into the working skein

> ## ⇦ READ THIS FIRST — settled with Patrick 2026-07-07
> - **Direction:** the WORKING skein (`skein/` + 8001 API) is canonical. `skein_next`
>   is the LIVE public station but is being RETIRED. Re-home its server half into
>   `skein/`, rebuild the live station from `skein/`, redeploy, THEN delete `skein_next`.
> - **`brief-20260621-aeh9` is the REVERSE/ABANDONED plan** (port legacy *into*
>   `skein_next`). Do NOT follow it. It is closed as superseded.
> - **Store: FORK B is DECIDED** (Patrick: "we can't have 2 folio classes; dual paths
>   are death"). ONE folio store — the working `versions` rows — for both roles; grow
>   `skein/storage.py` with the federation schema. Fork A (a second flat store) is
>   REJECTED. Migrate the live corpus carefully via the `fidelity/` comparison harness,
>   tested before cutover (§4, §5 Stage 7a).
> - **Execution:** driven by a single session (spins + fells); may run as implementor and
>   switch to spins around ~300k tokens (§5, §10 #4).
> - **Pre-delete backup:** to `~/src` locally; nothing is deleted until Patrick
>   finalizes — drive right up to the backup, then stop (§5 Stage 8, §10 #7).
> - Still open for discussion: station slug model (§4.1), env/launch-verb naming (§10 #5),
>   station-ops CLI shape (§10 #6).

Status: DESIGN GATE — **felled clean** (3 two-genotype rounds, opus + codex) and
**discussed with Patrick 2026-07-07**; Fork B + the above locked, three items still
in discussion. Author: burr-0707. From `brief-20260707-0c1h`.
Builds on: `docs/PHASE_4_DESIGN.md` (§2 two-codebases, §8 reuse boundary, §10 open),
`finding-20260704-alma` (settled model), `finding-20260707-meql` /
`summary-20260707-z2f5` (the security hardening that must travel with the crypto),
`finding-20260707-z9mj` (the `signing.py` fork drift).

> This gate is a plan to be **felled and then discussed** before any code moves. It
> does not authorize a port. The single most expensive mistake in this codebase is
> confusing the two "skeins"; §0 and §2 exist to prevent that. Read them first.

---

## 0. North Star (get this right — the record is contradictory and it drifts agents)

Direction, confirmed by Patrick 2026-07-07 and `finding-20260704-alma`:

- The **working skein** (`skein/` + `client/cli.py`, the 8001 API server) is the
  **canonical destination**. Everything worth keeping is re-homed *into* it.
- **`skein_next` / interskein is being RETIRED.** After the re-home lands and the
  live station is rebuilt from `skein/` code and redeployed clean, `skein_next` is
  deleted.

Two traps that have each cost a full session:

1. **`brief-20260621-aeh9` is the REVERSE plan** — port legacy *into* `skein_next`,
   make `skein_next` canonical. It is still "open" but it is **abandoned and
   inverted**. Do NOT follow it. Its `AGENT_COORDINATION_PORT_DESIGN.md` targets the
   wrong destination.
2. **`skein_next` is two-faced.** Its *authoring* half is abandoned fat-client code.
   Its *server* half is the **LIVE PUBLIC STATION** (interskein.com read `:9001` +
   ingress.interskein.com write `:9101`). Calling the whole tree "abandoned
   reference code" is what makes agents delete a live service or build on the wrong
   base. Hold it as: **"`skein_next` is live but being retired."**

---

## 1. What this is, and its boundary

**This re-home** gives the working skein codebase the ability to run as a **public
station** — the ingress write server, the web read server, and the pure verify
libraries they depend on — over a **federation-capable store**, so that the live
interskein.com station can be rebuilt from `skein/` and `skein_next` can then be
deleted.

Phase 4 already re-homed the **authoring/publish** half (`skein/publish.py` + the
`POST /skein/publish` route). This gate is the **complementary server half**: the
surfaces that *receive*, *store*, *verify*, and *serve* published content.

**The load-bearing design commitment (the KEY DESIGN POINT from the brief):** the
working skein **codebase** must become able to run as **either** the private 8001
workbench **or** a public station (ingress + read over a federation-capable store),
selected **by config + data-dir** — exactly as `skein_next` runs both today. "Unify
the store" means grow `skein/storage.py` (or a sibling module it owns) to hold the
federation schema so a station built from `skein/` can use it. It is **NOT** "the
8001 workbench process becomes the station."

### In scope
- Re-home ingress (`:9101` write server), web read (`:9001`), and the pure verify /
  manifest / envelope / rendering / auth / redeem libraries into `skein/`.
- Grow the working store with the federation schema (manifests, bindings, invites,
  attribution, verify-cache) and the station-facing accessor methods.
- Re-create the **station launchers** (today's `interskein serve` / `ingress` /
  `maintenance` verbs) on `skein/` code, and repoint the live Dockerfile CMD + ingress
  compose `command:` onto them.
- **Migrate the live production corpus** to the destination schema if Fork B is chosen
  (§4, §5 Stage 7a), non-destructively.
- Rebuild the live station image from `skein/`, redeploy interskein.com + ingress onto
  it, verify clean.
- Delete `skein_next` and its duplicated forks **after** the above verifies, with every
  re-homed surface's tests ported and green first.

### Out of scope
- Instance↔instance peer federation (`/fed/v0`) — still aspirational, its own later
  phase (consistent with `PHASE_4_DESIGN.md` §1).
- Re-thinning `skein_next`'s local **authoring** verbs — those are **dropped** (§3),
  not re-homed. The working skein already authors over its API.
- The agent-coordination port (roster/lifecycle/intranet as folios) — that is
  `brief-20260621-aeh9`'s subject and a **separate, currently-abandoned** track. This
  gate re-homes the **station**, not the coordination layer.
- New station features. This is a re-home: behavior-preserving on the wire and on the
  read surface, additive to the working store.

(The operator/invite/redeem station-operations CLI — needed to boot and run a signed
station — is explicitly **in scope**, RE-HOME in §3; its surface shape is the only open
call, §10 gated #6.)

---

## 2. System model — the two stores (this is the crux)

Two codebases and, more importantly for this gate, **two folio storage models**.

### 2.1 The working skein store (`skein/storage.py`) — the destination
- Folios are **`versions ⋈ refs`**. The legacy flat `folios` table was **retired**
  (Phase 3a A5). `versions` is a content-addressed immutable object store; `refs` is
  the mutable **naming + lineage + local-workflow** layer only — `slug`, `site_id`,
  `target_agent`, `omlet`, `acknowledged_at`, `metadata`. **Status and assignment are
  NOT on `refs`.** Since the threads-only contraction (skein `40fa961`, 2026-07-08)
  control state is **thread-derived**: genesis-keyed `status`/`assignment` control
  threads, reduced by `get_latest_statuses`/`get_latest_assignments` and overlaid on
  every folio-returning read (`enrich_folios_with_status` / `_overlay_thread_control`).
  The `''` invariant holds — a present empty value is state; defaults apply only on
  absence. The old `refs.status/assigned_to/archived` cache columns are gone.
- **Threads: the base DDL a *fresh* db is born with is `thread_id TEXT PRIMARY KEY`
  with a *separate, nullable* `thread_hash` column** (`skein/storage.py:480–488`). The
  3b `thread_hash`-PK swap lives in migration code
  (`skein/migrations/threads_pk_swap.py`), NOT in the base DDL — the live workbench db
  was migrated, but a *fresh* station db born from base DDL is **pre-swap**.
  `save_thread` handles both regimes, but on a pre-swap db **content-address dedup is
  lost**: a byte-identical re-received thread inserts a duplicate row and
  `get_thread_by_hash` (`skein/storage.py:1129`) then returns an arbitrary one. Stage 1
  must resolve this (define station-ready threads DDL, or run the migration on every
  station data dir before ingress boots).
- Sidecar tables today: `logs`, `screenshots`, `sacks`. **Zero federation tables.**
- No web read server in-tree; no ingress; no `verify_wire_*` orchestration.
  (`skein/web/` and `skein/mesh/` hold only stale `__pycache__` — their sources were a
  *different*, deliberately-dropped legacy read surface, commit `6850705`. Do NOT
  resurrect those; re-home `skein_next`'s.)

### 2.2 The `skein_next` store (`skein_next/store.py`) — the source
- Folios in a **flat `folios` table**: `(content_hash PK, type, created_at,
  created_by, title, content)` — all nullable `TEXT` (`store.py:148–155`).
- Threads in a `threads` table keyed by `thread_hash`.
- Naming via `slugs` + `aliases`.
- **Federation tables** (what the working store lacks): `manifests`,
  `constituent_attribution`, `account_bindings`, `binding_events`, `invites`,
  `invite_events`, `verify_cache` (`store.py:182–296`). (Also `sacks`, already in
  `skein/`.)
- This is the on-disk schema of the **live production corpus** — the read app opens it
  via `SkeinNextStore` (`web/app.py:48,212`) at `.skein-next/store.db`
  (`store.py:83–84`).

### 2.3 The finding that makes this feasible, not a rewrite — stated precisely

The working skein's `versions` columns and `skein_next`'s `folios` columns share the
**same six column names and the same canonical hashed field set**
(`content_hash, type, title, content, created_at, created_by`). `canon.py` /
`identity.py` are **content-hash-equivalent** (they differ only in docstring module
paths; hashing is byte-for-byte the same), so a folio's `content_hash` computes
identically on both sides — a received wire folio's identity survives the move.

They are **NOT identical schemas**, and the deltas bite exactly at the corpus
migration (§5 Stage 7a): `versions` declares `NOT NULL` on all five value columns and
`created_at DATETIME` (NUMERIC affinity) (`storage.py:566–570`), while `folios` is
nullable `TEXT` (`store.py:148–155`). Valid wire folios can't be null (wire hashing
rejects malformed field shapes, `wire.py:98–118`), so the mapping is clean on the
happy path — but a legacy row with a NULL title/content or a numeric-looking
`created_at` would fail or change affinity on insert into `versions`. The migration
must **validate/coerce**, not `INSERT ... SELECT` blindly.

Net: the re-home is **grow the store + re-point the servers + migrate the corpus**,
not a store rewrite. But "1:1 map" is only the write-side happy path; the read side
and the migration carry real work (§4, §5).

---

## 3. The inventory (verified against code — re-partitioned per fell round 1)

Fell round 1 found the first-draft partition **misfiled live server dependencies**.
The corrected partition below splits files that are part-keep, part-drop.

### RE-HOME (real functionality the working skein lacks)
| Surface | File(s) | ~lines | Why |
|---|---|---|---|
| Ingress write server (`:9101`) | `skein_next/ingress.py` | 667 | receive → verify → store signed publishes |
| Web read server (`:9001`) | `skein_next/web/app.py` + templates/static | 808 | public read + provenance render |
| Provenance render | `skein_next/render.py` | 382 | folio/provenance HTML |
| Envelope / verdict | `skein_next/envelope.py` | 596 | `folio_verdict`, wire envelope verify |
| Verify orchestration | `skein_next/sign.py` | 565 | `verify_wire_manifest`/`redeem`/`folio` |
| Admission auth | `skein_next/authorization.py` | 121 | `can_write`/bindings/revocation |
| Collaborator onboarding (server) | `skein_next/redeem.py` (+ invites) | 254 | invite redeem ceremony |
| Federation store schema + station accessors | (into `skein/storage.py`) | — | manifests, bindings, invites, attribution, verify_cache + the §4 contract |
| Plumbing | `profile.py` (140), `wire.py` (140), `resolve.py` (128), `stationfile.py` (269), `mesh/` | ~700 | domain sep, wire shape, resolution, station config |
| **Station store/runtime factory** (extracted from `station.py`) | part of `skein_next/station.py` | — | ingress opens the store through `Station(data_dir)` (`ingress.py:40,435`); the **read server opens `SkeinNextStore(...)` directly** (`web/app.py:212`, its own distinct rewire site). Both store-open paths are RE-HOME; the authoring verbs are DROP |
| **URL canonicalizer** (extracted from `publish.py`) | `canonical_instance` in `skein_next/publish.py` | — | ingress redeem-origin canonicalization (`ingress.py:466,469`) — this one symbol is RE-HOME; the rest of `publish.py` is JUST-DELETE |
| **Station launchers** (extracted from `cli.py`) | `serve`/`ingress`/`maintenance` group in `skein_next/cli.py` | — | the LIVE launch path has **two** points: the **read** container's Dockerfile CMD is `interskein … serve` (`Dockerfile:50`); the **ingress `:9101`** runs as a *separate compose service* whose `command:` invokes `interskein … ingress` (`PUBLIC_INGRESS_OPERATIONS.md`). `cli.py:669` → `web.app.run_server`, `cli.py:958` → `ingress.run_server`, `cli.py:1360` → `backfill_verify_cache`. Re-create as `skein.station` console-scripts / `python -m`; repoint **both** the Dockerfile CMD and the ingress compose `command:` (Stage 7b) |
| Station launcher (mesh) | `mesh = skein_next.mesh.cli:main` entrypoint | — | `pyproject.toml:63` — re-home with `mesh/` |
| **Station operations CLI** (extracted from `cli.py`) | operator + invite + redeem verbs in `skein_next/cli.py` | — | a signed station **cannot boot or onboard without these**: `require_signed` refuses to start unless exactly one active operator exists (`count_active_operators`, `ingress.py:437`), pointing operators at `account init-operator`/`rotate-operator`/`revoke` (`cli.py:1050–1118`) and invite `mint`/`list`/`revoke` (`cli.py:1226–1333`); the collaborator `redeem-invite` client (`cli.py:900`) onboards them. These are **operator-facing station ops**, distinct from the dropped *authoring* verbs — RE-HOME them (as `skein station account/invite/redeem` verbs) |

Server + verify libs sum to ~3–4k lines we keep. `store.py` (1771) mostly duplicates
primitives the working store already has (folios≡versions, threads, slugs) — only the
federation sidecars + station accessors are net-new.

### JUST DELETE (duplicated forks — the working skein already has these; they die *with* `skein_next`, nothing moves)
`skein_next/`: `canon.py`, `identity.py`, `words.py`, `shard.py`,
`publish.py` (except the `canonical_instance` symbol, RE-HOME above).

- **`canon.py` / `identity.py`** are **content-hash-equivalent, not byte-identical**
  (they differ only in docstring module paths). The working `skein/` copies are the
  survivors; hashing is unaffected.
- **`signing.py` is NOT "nothing moves" — it is *port-hardening-then-delete*.** The
  live-path hardening lives only in `skein_next/signing.py`, and the working
  `skein/signing.py` is the un-hardened copy (§4.2). Relocate `skein_next`'s hardened
  bytes into `skein/signing.py` **first** (Stage 2, §4.2), then delete the `skein_next`
  original. Deleting it as a plain fork would drop the hardening.
- **`address.py` is NOT a clean just-delete** — it is a **diverged fork**, and
  rewiring the re-homed surfaces onto `skein/address.py` is a **behavior change** on a
  live surface, not a byte swap. `skein/address.py` is a hardened superset (32 defs vs
  27); `resolve.py` (RE-HOME) imports `skein_next.address` (`resolve.py:32`) and
  `mesh/client.py` imports it (`mesh/client.py:89`), and `resolve_to_hash` is on the
  live read path (`web/app.py:559,602,630`). Re-homed `resolve.py`/`mesh` must retarget
  to `skein.address` **with the ported `test_resolve`/`test_web` proving parity** for
  short-hash / `web::` resolution before Stage 5 merges. (Filed separately from
  canon/identity for this reason.)

### DROP (abandoned fat-client authoring — the working skein already does this over its API)
`skein_next/`: `cli.py` authoring/publish/roster verbs (the `serve` / `ingress` /
`maintenance` launchers **and the `account` / `invite` / `redeem-invite` station-ops
verbs** are RE-HOME above), `station.py` authoring verbs (the store/runtime factory is
RE-HOME above), `roster_cli.py`, `shard_cli.py`, `bridge.py`, `agent.py`,
`legacy_meta.py`, `tender.py`.

Verified fork discipline: `skein/` runtime code never imports `skein_next` (only a few
doc-comments in `skein/publish.py`, all non-imports). One **test** imports
`skein_next.wire` (`tests/test_phase4_publish.py:395`) — must be repointed to the
re-homed wire before deletion (§8).

---

## 4. THE central design decision — store unification (Patrick-gated)

The **store API contract** the federation surfaces call (extracted from
ingress/web/sign/envelope/authorization/redeem) — the re-home must present it:

```
create_folio, get_folio, count_folios, list_folios, search_folios, folios_in_site,
folio_site_slug, folio_site_slugs, set_slug, list_slugs, resolve_slug, resolve_alias,
save_thread, get_thread, get_threads, latest_statuses,
add_manifest, all_manifests, add_constituent_attribution, get_constituent_proof,
verify_cache_get, verify_cache_put,
get_binding, count_active_operators,
get_invite_by_token_hash, redeem_invite_cas, reserve_redeem_attempt, log_redeem_failure,
transaction, savepoint, close
```

This is `SkeinNextStore`'s API. The working store does **not** expose these names —
its writer is `save_folio` (head-ref machinery, `storage.py:1379`), and the read
methods that *share* a name are **refs-coupled**: `get_folio` resolves a slug via
`refs` and returns the lineage head (`storage.py:1593–1601`), `search_folios` requires
the refs head-join (`storage.py:1684`), counts/lists go through `refs`. That coupling
is the crux of the fork below.

### Fork A — vendor the flat store (leanest, weakest unification)
Lift `SkeinNextStore` into `skein/` (e.g. `skein/station_store.py`), rewired to import
`skein/canon` + `skein/signing` + `skein/identity` (deleting the forks). A station
runs on this flat store; the workbench keeps `versions ⋈ refs`. One codebase, two
store classes.
- **Pros:** smallest diff; the station store stays byte-for-byte what runs live, so the
  live corpus is schema-compatible — **no corpus migration, `byte-unchanged` holds,
  rollback is trivial**; fastest path to "delete `skein_next`."
- **Cons:** does not literally satisfy "grow `skein/storage.py`" — it re-homes a
  *second* store rather than unifying; two folio models persist inside `skein/`.

### Fork B — grow the working store (true single store)
Add the 7 federation sidecar tables to `skein/storage.py`, and add **refs-free station
accessors** that satisfy the contract against the working tables: `create_folio` →
`versions` insert; `get_folio`-by-hash / `search` / `list` / `count` /
`folios_in_site` → re-implemented against `versions` + a `station_slugs` table (§4.1),
**not** the refs/head machinery. `save_thread` → `threads` (post Stage-1 DDL fix).
- **Pros:** literally the KEY DESIGN POINT — one store, config/data-dir selects
  workbench vs station; the folio *write* side is a mechanical `versions` insert (§2.3);
  kills the two-folio-model drift for good.
- **Cons (materially larger than the first draft admitted):**
  1. **The read contract SHRANK (threads-only contraction, skein `40fa961`, 2026-07-08).**
     The earlier draft called for a whole parallel refs-free read layer. That is now
     smaller: the workbench read layer is *itself* thread-derived — control comes from
     `get_latest_statuses` / `_latest_control_by_folio` and is overlaid by
     `enrich_folios_with_status` / `_overlay_thread_control` on every folio read; it no
     longer lives on `refs`. **Stage 1 should REUSE this shared derivation, not build a
     parallel control path** (same one-truth principle as the genesis-anchored claims +
     derived-heads slug model in `brief-20260708-31bu`). What genuinely stays net-new is
     slug/site resolution — station content lives outside the refs/head *lineage*
     machinery. Giving station content `refs` rows to reuse the head logic is still the
     Risk-3 trap (workbench lineage firing on received content); the dedicated
     `station_slugs` table (§4.1) is the way around it.
     - **Schema-straddling pattern (from the contraction fell — cite this for any
       re-home tool that must run across both the pre- and post-contraction schema):**
       `verify_threads_control` (the A3 oracle) gates on the DB **schema**, and when the
       removed archive reader is absent it runs the *identical* reduction **inline** (a
       frozen copy via `_latest_control_by_folio`), so it neither depends on the old
       reader nor silently mis-reports. This post-fell form supersedes the earlier
       tolerant-`getattr` sketch — straddle by schema-gating + an inline frozen reducer,
       not by defensively probing for a maybe-present method.
  2. **A one-time live-corpus schema migration** (`folios → versions+refs-or-station_slugs`,
     `slugs → station_slugs`, sidecars carried), non-destructive, tested — see Stage 7a.
     This is Fork B's single biggest practical cost and it is what makes the §6
     "byte-unchanged" invariant and the §7 rollback more complex.

**DECIDED: Fork B (Patrick, 2026-07-07 — "we can't have 2 folio classes; dual paths are
death").** ONE folio store — the working `versions` rows — for both roles; Fork A (a
second flat store class inside `skein/`) is **rejected**. Fork B is the only option that
delivers the KEY DESIGN POINT and ends the two-model drift; the §2.3 finding keeps its
*write* side mechanical. The accepted cost is the live-corpus migration (§5 Stage 7a),
which must be run **carefully via the `fidelity/` golden-master comparison harness**:
characterize the live station's read outputs on the old build, then prove the
`skein/`-based station returns byte-identical outputs against the migrated corpus before
cutover.

### 4.1 Sub-decision (Patrick-gated): station slug/site model
Under Fork B the station stores multi-author content with **no local authoring
lineage**; `site_slugs` on the wire is a flat `{site content_hash: slug}` map. Reuse
`refs` (accepting inert lineage columns and the Risk-3 hazard) or add a dedicated
`station_slugs` table? Recommend the dedicated table so station semantics never leak
into workbench lineage logic (§10 #2).

### 4.2 Hard requirement regardless of fork — the crypto hardening must travel (with an explicit merge direction)
`finding-20260707-z9mj` + `-meql`: `skein/signing.py` and `skein_next/signing.py` have
**diverged** — `skein_next`'s copy is the live-path, **hardened** one
(`verify_multi` catches `EmptySignatureBundle → BUNDLE_MALFORMED`,
`skein_next/signing.py:1471`; `skein_next/sign.py` has the single-signer guards for
manifest/redeem/folio at `:332,:447,:530`), while `skein/signing.py`'s copy is
**unreachable dead code** that catches only `ValidationError` (`skein/signing.py:1467`)
and lacks all three guards. The re-home **makes `skein/`'s crypto reachable** (the
station runs on it). Therefore, stated with an unambiguous direction so no one keeps
the wrong bytes:

> **The surviving `signing.py` is `skein_next`'s *hardened* `verify_multi`**, plus the
> `sign.py` / `envelope.py` single-signer and `verify_wire_folio` totality guards,
> **relocated to `skein/`'s path**. `skein/signing.py`'s current (un-hardened) bytes
> are **discarded, not preserved.** A re-home onto the existing `skein/signing.py`
> silently re-opens the fixed DoS.

Pin it with the ported `meql`/`z9mj` **failure-injection tests, which must still
FIRE** (single-signer reject, empty-bundle → `BUNDLE_MALFORMED`, folio-verify
totality). Stage 2 patches `skein/signing.py` **before** any server stage builds on it.
This also resolves `PHASE_4_DESIGN.md` §10 open #1 (fork consolidation).

---

## 5. Staging

Each stage is its own shard with its own fell. Order keeps the working 8001 workbench
correct at every step, and `skein_next` is deleted last.

**Stage 0 — settle the fork (this gate + discuss).** No code. Fell this plan
(two-genotype), discuss with Patrick, lock Fork A vs B and the §10 gated calls.

**Stage 1 — grow the store (the load-bearing primitive).** Under the chosen fork: add
the federation schema + station accessors to satisfy the §4 contract; **fix the
threads DDL** (§2.1) so a fresh station db is post-swap-consistent; under Fork B build
the refs-free read layer (§4 cons #1). Additive — the workbench tables/behavior are
untouched and the 8001 server keeps passing its full suite. Independent-shadow tests
for anything merkle/membership-adjacent. RSP-shaped — fell it hardest.

**Stage 2 — re-home the pure verify/crypto libs.** `sign.py`, `envelope.py`,
`profile.py`, `wire.py` into `skein/`, on `skein/canon` + `skein/signing` +
`skein/identity`, **carrying the §4.2 hardening (skein_next's bytes win)**. The ported
`profile.py` must keep recognizing the `skein.folio.canon/v1` profile (the 2026-06-07
knurl-1.0 → "SIGNATURE INVALID — unknown profile" incident,
`interskein-web/README.md:101–110`) — pinned by the §6 flagship-specs check. Port
`skein_next`'s verify suites (`test_verify_manifest`, `test_sign`, `test_envelope`,
`test_manifest`, `test_profile`, `test_require_signed`, the single-signer + totality
injections). Libs + tests only, no server.

**Stage 3 — re-home ingress (`:9101`).** The write server onto the Stage-1 store +
Stage-2 verify, with the extracted `Station` store factory and `canonical_instance`
(§3). Port `test_e2e_publish`, `test_manifest_store`, `test_require_signed`,
`test_ingress_http`, `test_ingress_concurrency`. Keep the accept-and-flag relaxation
exactly as live (`meql`/Phase-4 already shaped it).

**Stage 4 — re-home web read (`:9001`) + render.** The read server + `render.py` +
templates/static onto the Stage-1 store (read-only). Provenance verdict via the
re-homed `envelope.folio_verdict`. Carry the `meql` read-surface fixes (HTML
Cache-Control/ETag, verify-identity-for-display, `created_by`-is-a-claim byline). Port
`test_web`, `test_web_wire`, `test_render`, `test_read_old_schema`.

**Stage 5 — re-home admission auth + redeem + resolution + station config.**
`authorization.py`, `redeem.py`, invites, `resolve.py` (retargeted to `skein.address`,
§3), `stationfile.py`, `mesh/`. Carry the `meql` finding-6 fixes (redeem refuses
non-author before burn; single-`BEGIN IMMEDIATE` bootstrap). Port
`test_authorization`, `test_authz_cert_issuer`, `test_invite_redeem`(+`_hardening`,
`_property`, `_migration`), `test_verify_cache`, `test_resolve`, `test_stationfile`,
`test_profile`, `test_mesh`, `test_store`(+`_slice2`).

**Stage 6 — assemble station launchers + config toggle + local end-to-end.** Re-create
the `serve`/`ingress`/`maintenance` launchers on `skein/` (§3) and the config/data-dir
toggle so one build runs the workbench (8001) OR a station (ingress `:9101` + read
`:9001`). The station-ops `account`/`invite`/`redeem-invite` verbs (§3) land here too
(surface shape per §10 #6). **Define the exhaustive env/config-key mapping** — `SKEIN_NEXT_DATA_DIR`,
`SKEIN_NEXT_ORIGIN`, `SKEIN_NEXT_REQUIRE_SIGNED` (`ingress.py:385`), and the read
surface's `SKEIN_NEXT_PROJECT` / `SKEIN_NEXT_AUTHORITY` / `SKEIN_NEXT_BASE_URL`
(`web/app.py:68–83`), plus the stationfile → new names, or preserved verbatim (§6,
§10 #5). Full local round-trip: login-token → publish from an 8001
workbench → station ingress verifies+stores → content queryable + provenance-correct on
the station read surface. Full regression green in both roles.

**Stage 7 — rebuild the live image + redeploy (Patrick-gated cutover).**
- **Stage 7a (Fork B only) — live-corpus schema migration.** A tested,
  **non-destructive** converter: writes a **new** DB file in the destination schema and
  **leaves the flat `store.db` intact** as the rollback corpus. `content_hash` is
  preserved (canon agrees), so identity is safe. Independent-shadow check on the merkle
  set, **plus the `fidelity/` golden-master harness**: bless the live station's read
  outputs on the old (`skein_next`) build, then prove the `skein/`-based station returns
  byte-identical read outputs against the migrated corpus (this catches the silent
  capability-drop class the harness exists for). Run against a **copy** first; verify
  folio+thread hashes, slugs, manifests,
  attribution, account_bindings, **binding_events**, invites, **invite_events**,
  verify_cache, operator count, and the flagship reads before touching the live box.
  The two `*_events` tables are **append-only audit** (`store.py:227,269`) — their row
  counts, order, and contents must survive the migration. (Under Fork A this stage does
  not exist.)
- **Stage 7b — build + ship + recreate.** Build the station image from `skein/`
  (replacing the `skein_next`-based image), **repoint BOTH launch points** — the read
  container's Dockerfile CMD (`interskein … serve`) and the **ingress compose service's
  `command:`** (`interskein … ingress`) — onto the new `skein.station` launchers (§3);
  the verb rename is a cutover-lockstep alongside the env keys (§10 #5). Ship, recreate
  ingress + read, verify against the §6 live checklist. `require_signed` posture
  preserved.

**Stage 8 — delete `skein_next`.** Only after Stage 7 verifies clean and holds through
a stated live-hold window. **Hard precondition: every test whose target is a re-homed
surface is ported and green** (§8) — no `skein_next` test is deleted until its re-homed
twin passes. Then delete the RE-HOME sources' `skein_next` originals, the JUST-DELETE
forks, the DROP authoring tree; remove the `interskein` + `mesh` `skein_next`
entrypoints (`pyproject.toml:62–63`); repoint `tests/test_phase4_publish.py:395` off
`skein_next.wire`. **Backup first to `~/src` locally** (Patrick, 2026-07-07); nothing is
deleted until Patrick finalizes — drive right up to the backup, then stop for the go/no-go.

---

## 6. Deploy / cutover ordering (the constraint that pins the whole plan)

`skein_next` **is the live public station**, so it cannot be deleted until a station
built from `skein/` has replaced it in production. Ordering is non-negotiable:

1. Re-home lands in `skein/` (Stages 1–6), full suites green.
2. (Fork B) the live corpus is migrated non-destructively on a copy and verified
   (Stage 7a).
3. A station image is built from `skein/`, Dockerfile CMD repointed, redeployed, and
   **verified clean** (below), with the migrated corpus mounted (Fork B) or the
   existing corpus mounted (Fork A) and `require_signed=1` still enforcing.
4. **Only then** `skein_next` is deleted (Stage 8), tests ported first.

**Live verify checklist (from `summary-20260707-z2f5`, the cutover gate):** both
containers healthy; `require_signed=1` enforcing; the single-signer DoS guard present
in all three verify entrypoints (manifest + redeem + folio); `RequireSignedConfigError`
present; the flagship specs + hello-world folio still read `SIGNED`/verified (no
identity-mismatch regression; live `verify_cache` had 0 null-identity rows); an
unsigned folio is rejected; **the corpus *content-hash set* is unchanged** (Fork B
migrates the container, so "byte-unchanged" is replaced by "same content_hash set;
pre-migration DB retained"); **the env/stationfile keys resolve to the right data dir +
posture** (§10 #5); **both launch points** (read Dockerfile CMD + ingress compose
`command:`) start the new station; the ported profile registry still recognizes
`skein.folio.canon/v1` (else the flagship specs flip to "SIGNATURE INVALID — unknown
profile").

**Deploy mechanics** (per `~/production/digitalocean/interskein/`
`PUBLIC_INGRESS_OPERATIONS.md` + `interskein-web/README.md`): docker build locally →
`docker save | ssh | docker load` → recreate containers. DO droplet `45.55.249.33`
(root ssh). **Live mount RESOLVED (docker inspect, 2026-07-08):** BOTH live containers
mount **`/srv/interskein/corpus-staged`** — ingress `:rw`, read `:ro` via
`INTERSKEIN_CORPUS`; **`/srv/interskein/corpus` is NOT mounted** by either container.
This reverses the earlier assumption (that `corpus` was the live mount and
`corpus-staged` only a `publish.sh` pre-promotion staging copy): the live serving path
is `corpus-staged`, so **Stage 7 targets `corpus-staged`** — migrate a copy of it,
promote, then repoint. Predeploy backups are `/srv/interskein/corpus/store.db.bak-predeploy-*`;
compose backups on the box; retain the prior `interskein:public-20260707b` image for
rollback.

---

## 7. Rollback

- **Per-stage (1–6):** each stage is additive and shard-isolated; a bad stage is
  reverted by discarding its shard before merge. The working 8001 workbench never
  depends on the re-homed station code until Stage 6, so a mid-re-home abort leaves the
  workbench fully functional.
- **Cutover (Stage 7):** live rollback is `docker` recreate onto the retained
  `interskein:public-20260707b` image + the predeploy backups — the path `z2f5`
  exercised. **This only works if Stage 7a is non-destructive:** the migration writes a
  new DB file and leaves the original flat `store.db` intact, so the old `skein_next`
  image can still read the corpus on rollback. An in-place migration would break the
  rollback and is prohibited. Because `skein_next` is not deleted until Stage 8, the old
  station build stays buildable through the entire cutover window.
- **Fork-B write hazard (the write server is one-way):** the new ingress writes only to
  the new-schema DB, so a rollback to the old image + retained flat `store.db` would
  **drop any publish/redeem accepted in the verify/hold window**. Therefore the Fork-B
  cutover **quiesces the ingress route** — take `ingress.interskein.com` dark (the
  ops-doc nginx symlink-removal rollback) during the verify window — and is
  **forward-only once the first live write lands**. The **read** surface can still roll
  back freely; only the write surface is one-way. (Under Fork A the schemas match, so
  this hazard does not arise.)
- **Post-delete (Stage 8):** guarded by the pre-delete backup (frozen bundle, per the
  `skein-legacy` precedent). Only taken after Stage 7 has held.

---

## 8. Test / fell discipline

- **Per surface, one fell** (the brief's lean directive — a bounded re-home of server +
  pure-verify code, not a rewrite; heavy fleets compound the two-codebase drift).
  Trivial stages: one reviewer. **Stage 1 (store primitive), Stage 2 (verify), Stage 3
  (ingress) are security/load-bearing → two-genotype (opus + codex)**, extra genotype on
  a contested finding.
- **Port EVERY test whose target is a re-homed surface — a hard Stage-8 precondition,
  not a nicety.** `skein_next/tests/` holds ~46 suites; beyond the ~6 named in Stages
  2–3, these cover re-homed surfaces and must move: `test_envelope`, `test_sign`,
  `test_render`, `test_resolve`, `test_stationfile`, `test_profile`,
  `test_authorization`, `test_authz_cert_issuer`, `test_invite_redeem`(+`_hardening`,
  `_property`, `_migration`), `test_verify_cache`, `test_web`, `test_web_wire`,
  `test_ingress_concurrency`, `test_ingress_http`, `test_store`(+`_slice2`),
  `test_read_old_schema`, `test_manifest`(+`_store`), `test_require_signed`,
  **`test_cli_account`, `test_cli_invite`** (they pin the operator-boot + invite
  invariants of the re-homed station-ops CLI, §3). A `grep test → re-homed module` is
  the enumeration checklist. The invite-redeem hardening/property suites pin the `meql`
  finding-6 fixes; the store + old-schema suites guard the Stage-7a migration.
- **Failure-injection tests must FIRE** after the move (a clean path proves no guard),
  especially the §4.2 crypto guards.
- **Independent shadow** for the merkle/membership math on the store side and the
  Stage-7a migration (as `PHASE_4_DESIGN.md` §9 required for publish).
- **Cap with `deep_code_audit`** on the re-home diff per surface (it earned its keep,
  `z2f5`). Lean, not heavy: run it as a cap, union real hits into the fell.
- **Do NOT touch** `compute_thread_hash` or `canon.py` hashing (the standing Phase-3/4
  constraint). The re-home is behavior-preserving on the wire and on identity.

---

## 9. Risks

1. **Fork-B live-corpus migration (new, highest practical risk).** A destructive or
   lossy migration breaks the live station and the rollback. Mitigation: Stage 7a is
   non-destructive (new DB file, flat DB retained), independent-shadow-checked, run on a
   copy first, gated by the §6 checklist.
2. **Un-hardened destination crypto (§4.2).** Re-homing verify onto `skein/signing.py`
   as-is silently re-opens the fixed DoS. Mitigation: the §4.2 merge-direction gate +
   ported failure-injection tests on Stage 2, before any server stage.
3. **Store-mapping subtlety under Fork B.** Station read/write paths must not invoke
   workbench lineage/supersedes/slug-as-head logic. Mitigation: dedicated refs-free
   station accessors (§4 cons #1, §4.1), behavioral pins against the current live wire +
   read output.
4. **Inventory misfiling (the round-1 trap).** DROP/JUST-DELETE files (`cli.py`,
   `station.py`, `publish.py`, `address.py`) carry live server fragments. Mitigation: the
   §3 re-partition extracts the launchers / store-factory / canonicalizer / resolver
   before anything is deleted.
5. **Silent test loss.** Deleting `skein_next/tests` wholesale drops ~40 suites.
   Mitigation: §8 hard precondition (port before delete).
6. **Live cutover regression.** Mitigation: §6 live checklist as a hard gate, retained
   rollback image + corpus backups, `skein_next` kept buildable until Stage 8.
7. **Scope creep into the coordination port.** `brief-20260621-aeh9`'s work is out of
   scope (§1).

---

## 10. What is decided here vs open

**Proposed (to confirm in the fell + discuss):**
1. Destination is the working skein; `skein_next` is deleted last, after live cutover
   (§0, §6).
2. Re-home = grow the store + re-point the servers + (Fork B) migrate the corpus, not a
   store rewrite, on the §2.3 same-field-set finding.
3. The re-partitioned inventory of §3.
4. Store unification via **Fork B** (grow `skein/storage.py`), Fork A as fallback (§4) —
   now a closer call given Fork A avoids the corpus migration.
5. The crypto hardening travels with an explicit merge direction (§4.2) — a correctness
   gate.
6. Staging + cutover ordering of §5–§6; per-surface one-fell, two-genotype on the
   load-bearing stages; port-before-delete for tests (§8).

**Patrick-gated:**
1. **Store fork — DECIDED: Fork B** (one folio store; grow `skein/storage.py`; Fork A
   rejected — Patrick, 2026-07-07). Migrate the corpus via the `fidelity/` harness (§4).
2. **Station slug/site model — STILL OPEN** — reuse `refs` vs a dedicated `station_slugs`
   table (§4.1). Leaning dedicated table: workbench slugs name a *folio* (lineage + head
   + status, single author); station slugs name a *site* over immutable multi-author
   content joined by `within` edges — same word, different meaning. One folio store
   (`versions`) still holds; only the naming diverges.
3. **Fork-B live-corpus migration + rollback posture** — confirm non-destructive
   (new-file) migration and the live-hold window (§5 Stage 7a, §7).
4. **Execution vehicle — DECIDED: a single session** (this one) drives the spins +
   fells. Option on the table: run as the implementor directly and switch to spins around
   ~300k tokens (Patrick, 2026-07-07).
5. **Env/config-key + launch-verb preservation** — the full live key set
   (`SKEIN_NEXT_DATA_DIR` / `_ORIGIN` / `_REQUIRE_SIGNED` / `_PROJECT` / `_AUTHORITY` /
   `_BASE_URL` + stationfile) and the `interskein serve|ingress` launch verbs either stay
   verbatim or get renamed with the live compose + Dockerfile + ingress compose
   `command:` changed in the **same** deploy (§6, Stage 7b). A cutover-lockstep hazard on
   both the env and the launch.
6. **Station operations CLI shape** — the operator (`account`), invite, and collaborator
   `redeem-invite` verbs are now RE-HOME (§3, required to boot/operate a signed station,
   not optional). Confirm their new surface — `skein station account/invite/redeem`
   console-scripts, or folded into the thin client — and that a re-homed redeem-client
   matches the publish-client precedent.
7. **Delete timing / backup — DECIDED:** back up to `~/src` locally; drive right up to
   (and including) the backup, then stop — nothing is deleted until Patrick finalizes
   after a live-hold period (Patrick, 2026-07-07).
8. **`brief-20260621-aeh9` — DONE: closed** as superseded/abandoned (Patrick,
   2026-07-07), the reverse plan; the top banner warns future agents off it.
