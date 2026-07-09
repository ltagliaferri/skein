# Stage 1 — store unification (the load-bearing primitive)

Sub-plan of `docs/STATION_REHOME_DESIGN.md` §5 Stage 1. Grow the working store so a
station built from `skein/` can run on it, **additively** — the 8001 workbench schema,
behavior, and full suite are untouched. RSP-shaped (fell it hardest, two-genotype).

Branch: `shard-stage1-store` off master. Federation accessors and the servers that use
them are LATER stages; Stage 1 delivers schema + the refs-free folio/thread/slug read
layer + the station threads DDL.

## Decisions locked (this session, with Patrick)

- **Fork B, one folio store class.** No second folio-store class (Fork A rejected). The
  station reads/writes the SAME `versions` table as the workbench; only the naming +
  control projections and the sidecar tables differ by role. "One truth, projections
  per role" — the same principle as the threads-only control contraction.
- **Role is a construction-time selector** (`config`/data-dir picks workbench vs
  station), so a given `LogDatabase`/`JSONStore` instance is one or the other. Station
  mode is additive: it creates extra tables and exposes extra (refs-free) accessors; it
  never alters the workbench tables or their DDL.
- **Threads DDL — option (b), station-specific post-swap DDL.** Do NOT bake the
  `threads_pk_swap` (patsh) outcome into the shared base DDL. A workbench db is still
  born pre-swap (`thread_id` PK, nullable non-unique `thread_hash`) — byte-identical to
  today. A **station** db is born post-swap (`thread_hash` as the content-addressed key)
  so a received byte-identical wire thread dedups instead of inserting a duplicate row.
  Rationale (Patrick, this session): don't duplicate the patsh migration behavior in the
  base DDL for the first pass; keep workbench db-birth strictly unchanged.
- **Station naming = a dedicated `station_slugs` table** (design §4.1 + `brief-20260708-31bu`),
  never `refs` (Risk-3: workbench lineage/head logic must never fire on received
  multi-author content). A claim is `(slug, anchor_hash = lineage GENESIS content hash,
  claimed_by, scope)`; resolution DERIVES the head by walking `supersedes` forward from
  the anchor over versions the station holds. Site slugs are the degenerate case
  (anchor = the site folio, no walk).

## Scope — IN

1. **Station schema** (mode-gated, created only for a station db): the 7 federation
   sidecar tables ported from `skein_next/store.py` verbatim-where-possible — `manifests`,
   `constituent_attribution`, `account_bindings`, `binding_events`, `invites`,
   `invite_events`, `verify_cache` — plus `station_slugs` and `aliases`. `versions` is
   SHARED (already exists); `threads` is the station post-swap shape; `sacks` already
   exists.
2. **Refs-free station folio accessors** over `versions` (+ `station_slugs`), matching
   the return shape the skein_next servers rely on (contract table below, from the
   research inventory): create_folio, get_folio(by hash), list_folios, folios_by_type,
   folios_by_created_by, search_folios, find_by_prefix, folios_in_site,
   folio_site_slug(s), latest_statuses. NONE of these touch refs/head/lineage machinery.
3. **Station thread accessors**: save_thread (content-addressed dedup on the post-swap
   DDL), get_thread(by hash), get_threads.
4. **Station slug accessors**: the `station_slugs` claim + genesis-anchored derived-head
   resolver; fold site slugs in as the degenerate case.
5. **`latest_statuses` is a refs-free, hash-keyed reduction over the one status-thread
   graph (option (b), decided with Patrick 2026-07-09)** — skein_next's exact form
   (`SELECT to_id, content ... type='status' ... ORDER BY created_at, thread_hash`,
   last-write-wins), NOT the workbench's refs-slug-keyed `_latest_control_by_folio`. Both
   roles derive from the SAME status-thread truth (the shared-derivation principle holds
   at the graph level); they differ only in key space (folio hash vs refs slug), so a
   forced shared reducer would need a keying shim and re-fell the just-contracted
   workbench path for no gain. See the "Two couplings" section below.

## Scope — OUT (later stages)

- Federation-table ACCESSORS (add_manifest / bindings / invites / verify_cache read+write)
  ride with their servers (verify libs Stage 2; ingress/read/auth later). Stage 1 lays
  the tables only.
- Wire-claim arrival for folio slugs, ingress admission checks, resolution caching.
- Any server (ingress, web read), the verify orchestration, the corpus migration.

## Threads DDL — the mechanism

A station `LogDatabase` created in station mode issues the post-swap `threads` DDL
(`thread_hash TEXT PRIMARY KEY`, the content address) instead of the base pre-swap DDL.
`save_thread` computes `compute_thread_hash` and inserts keyed on it, so a re-received
byte-identical thread is an idempotent upsert (INSERT OR IGNORE / ON CONFLICT), not a
duplicate. No data migration runs (a station db is born empty and fills from the wire).
The workbench path is not on this branch at all.

## Test design (write first)

- **T-station-schema**: a station db is born with all 8 sidecar tables + post-swap
  threads; a workbench db is born with NONE of the station-only tables and pre-swap
  threads. Assert `PRAGMA table_info` for both, byte-compare workbench DDL to master.
- **T-workbench-untouched (regression)**: the full existing suite stays green; a fresh
  workbench db is schema-identical to master (no new tables, threads still `thread_id` PK).
- **T-thread-dedup (station)**: saving a byte-identical wire thread twice yields ONE row;
  `get_thread(hash)` returns it; distinct content → distinct rows.
- **T-folio-shadow**: for each server-called folio accessor, a golden set of folios
  produces the SAME return shape/ordering as `SkeinNextStore` on the same inputs
  (differential test: build the identical corpus in both stores, assert equal results).
  Covers create/get/list/by_type/by_created_by/search/find_by_prefix/folios_in_site.
- **T-slug-derived-head**: a slug anchored to genesis resolves to the newest verified
  version reached by walking supersedes; a republish (v2 + supersedes edge) moves the
  resolution with zero mutable state; a fork resolves to a fork, never a silent winner.
- **T-latest-statuses-shared**: station `latest_statuses` == the working store's
  thread-derived reduction on the same thread set (shared code, not a reimplementation).
- **Independent-shadow** for anything membership/merkle-adjacent per §5 (the manifest /
  attribution tables are laid but not exercised in Stage 1; their pins arrive with the
  verify stage).

## Contract table (server-required accessors)

See `docs/STAGE_1_CONTRACT.md` — the exact server-reached method set (from the readonly
inventory, spool 71793ff1): ~13 store-live folio/thread/slug methods (dead ones skipped)
plus the deferred federation set, with the 12 subtle behaviors to differential-test.

## Increment split (each felled)

- **1a — schema + threads DDL** (this increment): the 8 mode-gated station sidecar tables +
  the station post-swap `threads` DDL (option b). Concrete, additive; schema + regression
  tests.
- **1b — folio/thread/slug accessors**: the refs-free Group A/B/C reads/writes over
  `versions`/`threads`/`station_slugs`, matching the contract doc's return shapes.
- **1c — control + naming**: `latest_statuses` (refs-free, hash-keyed — the skein_next form,
  NOT the workbench `_latest_control_by_folio`; see the open call below) and the
  genesis-anchored `station_slugs` derived-head resolver (brief-20260708-31bu).

## Two couplings found + how Stage 1 handles them (Risk-3 evidence)

- **`save_thread` genesis-keys via `_genesis_key_control` → `genesis_of_slug` (refs).** A
  station has no refs; the station thread write stores wire endpoints verbatim (already
  genesis-anchored by the sender), refs-free. Confirmed the workbench `save_thread` already
  does `INSERT OR IGNORE` on the hash, so on a post-swap (station) threads table dedup is
  automatic — the station variant just drops the refs genesis-keying step.
- **`_latest_control_by_folio` is refs-coupled** (JOINs an anchor_map built from `refs`,
  returns `{slug: value}`). The station keys control by folio HASH, refs-free. OPEN CALL for
  1c: (a) refactor a pure reducer out of the just-felled workbench method so both roles share
  it (true "reuse", but touches contraction code — re-fell), vs (b) give the station the
  ~10-line refs-free reduction (skein_next's form; a second projection of the one status-
  thread truth, not a second store). Leaning (b) to avoid re-felling the workbench path;
  flag to Patrick at 1c.
