# Phase 3 design gate — threads as content-addressed edges

Status: design note for review (two-genotype fell before any migration code).
Revision 2 — incorporates the first fell (opus + codex, both NOT-CLEAN; all
blockers/majors folded in). Parent: `brief-20260626-fmfs` (one content-addressed
model + the state-class taxonomy). Sibling: `docs/PHASE_1_2_DESIGN.md` (what
versions/refs shipped). Supersedes the abandoned `PHASE_3_STEP1_DESIGN.md`.

This note owns the Phase 3 boundary: turning the conflated legacy "thread" into
the object classes fmfs resolved — **ref-local control state** and
**content-addressed structural edges** — and finishing the `folios.status` drop
that Phase 2 deferred. It is RSP-shaped around fmfs; the research that backs it
is `finding-20260630-eq68` (live survey), `-w3ix` (external prior art), and the
locked decisions in `-24bk`.

---

## 1. What Phase 3 is, and what it is not

Per fmfs, Phase 3 = "threads as content-addressed edges (makes the graph
mesh-ready)." The load-bearing move is the **state-class taxonomy split** of the
single legacy `threads` table, which today conflates control state, structural
folio links, and agent-graph links under one slug-keyed row shape.

It is **not** "re-anchor status threads to genesis." Status is the trivial
fallout of one class; the real work is giving every edge a real content hash
over its endpoints so the structural subset becomes a signable, dedup-able
Merkle DAG.

The sequencing decision (settled with Patrick 2026-06-30): **state-first, with
the one `thread_hash` scheme designed once for all edges.** The control
re-anchor lays the content-addressing foundation using the *same* hashing rule
the structural edges will use, so it is not a throwaway re-key. The threads
table is touched twice (3a then 3b) — accepted as the most sensible split — but
the hash scheme is decided exactly once, and it already exists in code
(`compute_thread_hash`, §2.3). The class of a *non-control* edge is decided
**per-row by its actual endpoints**, not by its type name (§3.1) — the first fell
found the live data does not honor a clean type→class mapping.

---

## 2. Ground truth — what exists now

Verified first-hand against `master @ 276796f`.

### 2.1 The threads table (`skein/storage.py:502`)

```
threads(
  thread_id   TEXT PRIMARY KEY,   -- random slug 'thread-YYYYMMDD-xxxx'
  from_id     TEXT NOT NULL,      -- today a SLUG (folio_id) OR an agent id
  to_id       TEXT NOT NULL,      -- today a SLUG OR an agent id
  type        TEXT NOT NULL,
  content     TEXT,
  weaver      TEXT,               -- the agent who set the edge
  created_at  DATETIME NOT NULL
)
```

Indexes on `from_id`, `to_id`, `type`, `created_at`, plus the compound
`(to_id,type,created_at DESC)` / `(from_id,type,created_at DESC)` that the batch
status/assignment readers ride. The PK is a **random id, not a content hash.**
There is no `thread_hash` column.

Live type distribution (skein's own db; shape is representative): `reference`,
`status`, `mention`, `reply`, `tag`, `message`, `succession`. Plus `assignment`
(nearly dead — 3 rows ecosystem-wide) and the Phase-2 `supersedes`/`reverted`.

### 2.2 The type vocabulary (`skein/models.py:146`)

`ThreadType` is a `Literal`, **not** an enum:

```
message, mention, reference, assignment, succession, reply, tag, status,
supersedes,   # Phase 2: from_id = new hash, to_id = old head hash
reverted      # Phase 2: from_id = prior head hash, to_id = reused hash
```

It already documents that `supersedes`/`reverted` endpoints are **content
hashes, not slugs** — the first (and today only) hash-keyed edges in the table.
It lacks `archive`, `within`, and `published`.

### 2.3 The hash scheme already exists and is unwired

- `skein/identity.py:86` `compute_thread_hash(from_id, to_id, type, weaver,
  created_at, content)` → `sha256::<hex>`.
- `skein/canon.py` `thread_canonical_fields` / `thread_canonical_bytes` — the
  six-field canonical dict, `created_at` **normalized and included in the hash**,
  every other field `str|None` (TEXT-affinity safety). Same knurl canonicalizer
  as folios.
- **Zero callers in the live `skein/` package.** `save_thread` does not compute
  it; the PK is still the random `thread_id`. (`skein_next/` — reference-only,
  never run — does call it; that does not make it wired in the runtime.)
- `skein/canon.py` already builds an RFC-6962 Merkle manifest over a publish's
  constituent **folio AND thread** content hashes. The DAG's signing substrate
  is already in place and kind-agnostic.

So "design the thread hash" is really "wire the thread hash that is already
written and already assumed by the signing layer." This is the single biggest
de-risking fact in Phase 3.

### 2.4 The control read/write path

Writers (server-side, `skein/routes.py`): status self-loops at 848/853,
1016/1021, 2062/2067; assignment at 861/866, 1031/1036; the generic
`POST /threads` at 1118 → `save_thread`. **Insert paths beyond `save_thread`
exist:** `supersedes`/`reverted` are minted by direct `INSERT INTO threads`
inside `_maintain_versions_refs` (`skein/storage.py:1395` and `:1413`), which
never calls `save_thread`; and the REPAIR mode of the shipped backfill mints a
`supersedes` edge by direct insert (`skein/migrations/backfill_versions_refs.py:288`).
There are **four** insert sites in all — `save_thread` (`storage.py:1096`), the
two in `_maintain_versions_refs`, and the backfill-repair mint; any thread_hash
wiring must cover all four (§5.A2).

Readers: `skein/storage.py:1156` `get_latest_statuses` (groups `to_id` where
`type='status'`, `MAX(created_at)`), `:1188` `get_latest_assignments` (groups
`from_id` where `type='assignment'`); `skein/utils.py:79/88/97`
`get_current_status` / `get_current_assignment` / `enrich_folios_with_status`.

The reader carries the tiebreaker hazard w3ix flagged:

```
... AND t.created_at = latest.max_created AND t.type='status'
```

On two `status` rows sharing the max `created_at`, **both** rows survive the
join and the result dict keeps whichever lands last — non-deterministic. Live
probe: 0 equal-`(folio, created_at)` groups today, so it is latent, not a
current corruption. The fix rides along (§5.A1).

### 2.5 refs control columns + folios.status (the contraction target)

`refs` (`storage.py:658`) carries `status / assigned_to / archived`. **These are
currently a copy-of-column cache written from the folio object in `save_folio`
(`storage.py:1320-1345`), not a replay of the status/assignment threads.** That
is why `eq68` found 11 folios whose `refs`/`folios.status` cache disagrees with
the thread-derived live read — the cache can go stale; the live read
(`get_current_status`) already overrides it from threads. `folios.status` is
still dual-written by `save_folio` and is a write-only vestige since Phase-2
commit C. Rebuilding `refs` control **from threads** (§5.A3) both moves the cache
onto its real source of truth and corrects the 11 stale rows; it is invisible to
users because the live read already prefers the thread value.

---

## 3. The state-class taxonomy (the load-bearing classification)

### 3.1 Control class is decided by type; every other class is decided by endpoints

The first fell's blocking finding: the live data does **not** support a clean
type→class mapping. Sampling endpoint shapes across the project dbs, `reply` is
mostly `X→F` / `X→X` with only a small `F→F` subset; `succession` is `F→F` in at
least one db while being agent→agent elsewhere; `message`, `mention`, `tag`,
`reference` are all mixed by endpoint shape. So a row's class is a property of
**its endpoints**, established by audit, not assumed from its `type`.

Two questions classify a row:

1. **Is it control state?** (status / assignment / archive) — the server mints
   these on behalf of a lineage and they drive `refs` filters. Control wins
   regardless of endpoints: even though an `assignment`'s `to_id` is an agent
   (which would otherwise read as "non-folio"), control membership takes
   precedence. Control = **Class A**.
2. **For every non-control edge: are BOTH endpoints folios (resolvable to a
   content hash)?** If yes → **Class B** (federates, re-anchored to folio
   hashes). If any endpoint is an agent/session/non-ref token → **Class C**
   (stays slug/agent-keyed, does not federate yet). This is a **per-row** test,
   run in the 3b endpoint audit.

### Class A — ref-local control state (genesis self-loops)

Types (type-clean — the server controls every write): `status`, `assignment`,
`archive` (new).

- Anchored to the lineage's **genesis hash** (the first version's content hash =
  the stable lineage id, which never moves across edits). `status` and `archive`
  are self-loops (`from = to = genesis`). `assignment` is `from = genesis`,
  `to = assignee` (an agent — but it is still Class A by the control-precedence
  rule above).
- Source of truth lives in threads; denormalized into `refs.status /
  assigned_to / archived` as a **regenerable cache** (failure mode is rebuild,
  never corruption — threads are the answer key).
- **Local, never federated.**
- Read always resolves `folio → refs.genesis_hash → type-filtered threads`
  (invariant **I2**); never a bare-sha → lineage reverse lookup.

This is the **Phase 3a** scope — fully researched and decision-locked (`24bk`).

### Class B — content-addressed structural folio edges (the Merkle DAG)

Rows (not whole types): `supersedes`, `reverted` (already hash-keyed, shipped in
Phase 2), plus the `F→F` subset of `reply` / `within` / `published` and any
`F→F` rows of `reference` / `mention` / `tag` the 3b audit confirms.

- Both endpoints are **folio/version hashes**. An edge anchored to a *version*
  hash is version-scoped (`supersedes` pins exact head versions); anchored to a
  *genesis* hash it is lineage-scoped. Per-edge head-vs-genesis anchoring is a
  **3b** decision, per type, after the endpoint audit.
- Immutable, append-only, `thread_hash`-keyed; this is the signed subgraph that
  **federates**.
- Re-anchoring the confirmed-`F→F` rows from slugs to folio hashes is **Phase
  3b**. `supersedes`/`reverted` are already here.

### Class C — non-federating edges (at least one endpoint is not a folio)

Rows: `succession` (where agent→agent), `message`, and every
`reply`/`reference`/`mention`/`tag` row whose audit finds a non-folio endpoint.

- These **cannot** be "hash over endpoint folio hashes" — an agent/session is
  not a content-addressed object. fmfs explicitly keeps `succession` for agent
  lifecycle.
- They stay **slug/agent-keyed** through Phase 3. They gain a `thread_hash`
  (so the table is uniformly content-addressed and the row dedups by its own
  bytes), but their **endpoints are not re-anchored**, and **they must not enter
  the federated Merkle manifest** until/unless their endpoints resolve to hashes
  — putting a local slug on the wire is the dual-ID leak fmfs warns against
  (§7).

**End state is intentionally a mixed-keyed table** (`24bk` #1): Class A on
genesis hashes, Class B on folio hashes, Class C on slugs/agents — every row
content-addressed by `thread_hash`, but endpoints keyed by what the edge
actually connects.

---

## 4. The end-state schema and the dedup semantics

```
threads(
  thread_hash  TEXT PRIMARY KEY,   -- compute_thread_hash(...) ; the row's identity
  from_id      TEXT NOT NULL,      -- Class A: genesis hash; B: folio hash; C: slug/agent
  to_id        TEXT NOT NULL,
  type         TEXT NOT NULL,
  content      TEXT,
  weaver       TEXT,
  created_at   DATETIME NOT NULL
)
```

`thread_id` is retired as the PK. Whether the random `thread_id` column survives
as a non-identity audit handle or is dropped is a **3b** call (the PK swap is a
table rewrite, the second touch). **3a only adds `thread_hash` as a backfilled,
nullable, non-unique, non-PK column** — uniqueness enforcement (the `UNIQUE`
index / PK), the table-wide byte-duplicate collapse, and the audit of any
consumer that assumed every write yields a distinct row all land **together in
3b** with the PK swap. Imposing a table-wide `UNIQUE(thread_hash)` in 3a would
force-collapse non-control byte-duplicates (`reference`/`tag`/`message`) that 3a
otherwise never touches and that its control-only equivalence verifier (§5.1)
does not cover — so 3a keeps its blast radius to the control rows it actually
re-anchors and verifies.

**Dedup semantics — stated precisely (corrects a fell finding).** `thread_hash`
hashes all six canonical fields **including the normalized `created_at`**. So
content-addressing collapses only **fully byte-identical** rows — same
endpoints, type, weaver, content, *and* same normalized timestamp. It does
**not** make "re-assert the same logical state" idempotent: the live writers
stamp `created_at = now()` (`routes.py:844-866`, `1012-1036`, `2058-2099`), so
setting a status to `closed` twice produces two rows with different timestamps
and therefore two distinct `thread_hash` values — both persist. The only rows
that collapse are genuine byte-duplicates: a replayed import, a double-save
landing inside the same normalized instant, or two legacy rows that differ
*only* by their random `thread_id`. Because uniqueness is not enforced until 3b,
these byte-duplicates are harmless in 3a — the control readers reduce a folio's
rows to a single latest value regardless of duplicate identical rows, so they
never change a read — and they are collapsed when 3b takes the PK.

---

## 5. Phase 3a — control taxonomy + the folios.status drop

The researched, decision-locked slice. Expand/contract (Martin Fowler
ParallelChange; the canonical online-migration shape w3ix validated), one commit
per step, additive + green, no red intermediate. Two-genotype fell on every
load-bearing commit; the differential fidelity harness watches as the drift
alarm (legacy = answer key).

### A1 — expand: tolerant reader + new type + new column (additive)

- Add `archive` to `ThreadType` (`models.py:146`), and a **dedicated** archive
  read path — a `get_latest_archives` reader analogous to `get_latest_statuses`
  (a self-loop keyed on `to_id`, genesis-anchored) but reducing to
  `refs.archived`, **not** folded into `get_latest_statuses` (that reader maps to
  `refs.status`, a different column). 0 archive rows exist ecosystem-wide, so the
  reader exercises no data in 3a, but the path must exist and be correct before
  A4 makes reads genesis-only and A5 drops `folios.archived`.
- Add a nullable, **non-unique** `thread_hash` column to `threads`. No `UNIQUE`
  index in 3a (§4): uniqueness, the table-wide collapse, and the distinct-row
  consumer audit are 3b's, with the PK swap.
- Make the control readers tolerant of **both** keyings, as a **single reduction
  over the union** of a folio's slug-keyed and genesis-keyed control rows —
  *not* "prefer genesis rows, else fall back to slug." A folio mid-migration can
  have a slug-keyed `status` at T2 and a genesis-keyed one at T1; the reader must
  union both keyspaces, then take the latest by the deterministic order below.
  A prefer-genesis-else-slug rule would return the stale T1 value. The two
  control readers key on **opposite** columns and so need **two typed union
  branches**, not one generic reducer: `get_latest_statuses`
  (`storage.py:1156`) identifies the folio by `to_id` (the `status`/`archive`
  self-loop endpoint), so its union is `to_id ∈ {slug, genesis_hash}`;
  `get_latest_assignments` (`storage.py:1188`) identifies the folio by `from_id`
  (assignment is `from = folio`, `to = assignee`), so its union is
  `from_id ∈ {slug, genesis_hash}`. Each branch maps its rows back to the folio
  via the slug and its `refs.genesis_hash` and normalizes to the same
  `folio → value` output. A reducer that unioned the wrong column would silently
  drop one of the two control kinds.
- Land the **deterministic tiebreaker** now: order by `(created_at, thread_id)`,
  closing the equal-timestamp ambiguity (§2.4) before any data moves. The reader
  and the A3 rebuild must use the **same** order column (`thread_id`) — if the
  reader broke ties on, say, `thread_hash` while the rebuild used `thread_id`,
  the two would pick different rows on an equal-`created_at` group and the
  rebuilt-refs == live-read equivalence would break. Migrating both to a
  `thread_hash` order is a 3b option, taken for both together. Zero effect on
  current data (0 ties).

Green: reads return the identical value whether a folio's control is slug-keyed,
genesis-keyed, or split across both.

### A2 — writers emit genesis-keyed control + compute thread_hash everywhere (additive)

- The `routes.py` status/assignment writers and `save_thread` write control
  edges with **genesis-hash** endpoints going forward. The **archive writer is
  added here in A2** (its reader counterpart landed in A1): it mints a
  genesis-anchored `archive` self-loop and updates `refs.archived`. It has no
  current caller's data, but the writer must exist so the A1 read path it feeds
  is exercised by tests.
- Populate `thread_hash = compute_thread_hash(...)` at **all four** insert
  sites (§2.4): `save_thread` (`storage.py:1096`), the `supersedes`/`reverted`
  INSERTs in `_maintain_versions_refs` (`storage.py:1395`, `:1413`), and the
  REPAIR-mode `supersedes` mint in
  `backfill_versions_refs.py:288`. Missing any of them lets a new edit/repair
  edge land with `thread_hash = NULL`, which both violates §4 and blocks 3b's
  NOT-NULL PK swap. (If the backfill tool is instead frozen — not run between A2
  and the PK swap — say so in its header and in this step; do not leave it as an
  un-wired live insert path.)
- Old rows stay slug-keyed; the A1 tolerant reader covers them. Green.

### A3 — migrate: re-anchor existing control data (the risky step)

The one migration that rewrites busy thread data. Copy-proof: prove on a **copy**
of every real db first, then run live `--all --backup` with a full pre-migration
archive in hand (the `retire_folios_fts` archive convention). Per-db transaction
is `BEGIN IMMEDIATE` + `busy_timeout`; quiesce speakbot (busiest) per w3ix.

**Precondition (refuse the db if violated):**

- **I1**: `genesis_hash` unique per lineage (verified 0 collisions; the
  migration asserts it, never assumes it).

By A3 run-time the input is a **mixed keyspace**: the A2 writers have been
emitting genesis-keyed control since their deploy, so any status/assignment set
in the A2→A3 window is *already* in target shape (endpoints are genesis hashes,
not slugs). The transform must not re-resolve those. Classify each row by its
endpoint shape **first**:

- **already genesis-keyed** (the control endpoint is a 64-hex `sha256::`, in the
  normalized address form, that matches a live `refs.genesis_hash`): **pass
  through untouched** — it is already the target shape. A literal "resolve the
  slug against `refs.slug`" rule would fail to find a genesis hash in the slug
  column and wrongly drop a *current* status the A2 writer just wrote, reverting
  that folio to open in the A4 rebuild. This branch is the A3 analogue of the A1
  reader's both-keyspaces union. A genesis-shaped endpoint that does **not**
  match any live `refs.genesis_hash` (its lineage was deleted after A2 wrote the
  row) is a genesis orphan: **drop-but-logged as a genesis orphan** (distinct log
  category from a slug orphan), same disposition, accurate provenance. Both the
  match here and the slug resolve below compare the **same normalized `sha256::`
  spelling** used by `compute_folio_hash`, so a framing/prefix difference can
  never mis-route a live genesis row to the slug branch.
- **slug-keyed** (the legacy rows): apply the **resolve-genesis-or-drop** rule
  below.

The resolve rule, applied only to slug-keyed rows, reduces to one test that
subsumes the self-loop/non-self-loop/orphan distinction and handles their
overlap (a row can be both `from = agent` *and* point at a deleted slug — the 39
and 83 are independent `eq68` filters, so they can intersect and `~5203` is the
remainder net of any overlap, not an exact count). Resolve the folio slug each
row carries against `refs`:

- **unresolvable** (no ref = deleted folio): **drop-but-logged**, regardless of
  shape. This is the gate, so an orphaned non-self-loop row is dropped, never
  normalized to a `from = NULL` that would violate `from_id NOT NULL`.
- **resolvable**, `status`/`archive`: re-key to a genesis self-loop
  `from = to = genesis_hash`. If the row was non-self-loop (`from = agent`), the
  setting agent is preserved in `weaver` (`24bk` #2) — this is the same re-key,
  with the agent moved off `from`, not a separate rule.
- **resolvable**, `assignment` (`from = folio`, `to = assignee`): re-key
  `from = genesis_hash`, leave `to` (the assignee) untouched. The one
  `eq68` "odd-shape" assignment row is enumerated explicitly (3 rows exist total,
  so this is concrete) and normalized or dropped with a logged reason.

`eq68` magnitudes for sizing/verification (status 5325 = 83 orphan + 39
non-self-loop + ~5203 plain, overlap-adjusted; assignment 3 = 1 normal + 1
orphan + 1 odd; archive 0 — path added, no data).

Then, in order:

1. backfill `thread_hash` for **every** row (every type), computed over its
   **post-transform** canonical fields, into the non-unique column from A1.
2. **rebuild** `refs.status / assigned_to / archived` from the re-anchored
   threads using the deterministic `(created_at, thread_id)` reduction.

No `UNIQUE` index and no row collapse in 3a (§4). The re-anchor *does* manufacture
byte-duplicate control rows — a slug-keyed `status` and an already-genesis-keyed
`status` with the same `content`/`weaver`/normalized-`created_at` become
identical once both are `from = to = genesis` — but with no uniqueness enforced
they are harmless: step 2's latest-by-`(created_at, thread_id)` reduction picks
one value, and identical duplicates carry the same value, so `refs` is correct
regardless. The byte-duplicate collapse (whole-table, all types) and the
`UNIQUE`/PK enforcement run in 3b, alongside the audit of consumers that assumed
distinct rows. The copy run executes A3 first, and the §5.1 value-equivalence
verifier confirms the rebuilt `refs` equal the pre-migration live read before
anything goes live.

### A4 — contract: reader genesis-only (gated on A3 completion everywhere)

Drop the slug-key tolerance; reads become genesis-only via `refs.genesis_hash`
(I2). **Precondition (explicit gate):** A4 must not deploy until A3 has run on
**every** live db and a check confirms **zero dbs retain slug-keyed control
threads**. The ecosystem has 45 registered dbs; one offline or added between A3
and A4 would, under the genesis-only reader, read every non-open folio there as
"open" (`folio → genesis → to_id = genesis` finds nothing). It is repairable by
re-running A3, but it is a green violation, so the gate is a hard precondition.
Fresh dbs created after A2 are born genesis-keyed by the A2 writers and are fine.

Green once the gate holds: equivalence is exact.

### A5 — contract: drop folios.status and the folios table

Precondition guard (the collapsed old "step 2", `24bk`): assert **nothing lives
only in `folios.status`** — `nonopen_no_thread = 0` makes this a verified no-op,
re-checked live before the drop. Note the drop also discards
`folios.assigned_to / archived / target_agent / omlet / acknowledged_at /
metadata`; `eq68` verifies `assigned_to`/`archived` are empty ecosystem-wide and
the rest are mirrored in `refs`, so no data is at risk — but the guard should
assert that, not only the `status` column.

Then the contraction, in one commit:

- drop `folios.status`; drop the `folios` table; remove its DDL from `_init_db`;
  gate/remove `migrate_folios_from_json` (queries `folios`); stop the `folios`
  dual-write in `save_folio`.
- **Also stop `move_folio`'s separate `folios` write** (`storage.py:1517`,
  `UPDATE folios SET site_id ...`) — its own docstring calls it "a second
  folios-write path outside save_folio." Keep its `refs.site_id` write. Omitting
  this regresses `POST /folios/{id}/move` to a `no such table: folios` 500 the
  moment the table drops — a guaranteed green violation (this is the blind spot
  in `PHASE_1_2_DESIGN.md` §9.3, the contraction list A5 inherits).
- Remove the now-dead `FROM folios` legacy fallbacks (`storage.py:200/254/305`,
  guarded by `_has_refs_table`) — not a correctness break (they never execute on
  a backfilled db), but they would name a dropped table.

Reads have been off `folios` since commit C, so the data path is pure
contraction; the two writer paths (`save_folio`, `move_folio`) and the dead
fallbacks are the full surface to retire.

### 5.1 Equivalence definition and verifier

Correctness target (`24bk` #4): **all three** rebuilt `refs` control columns —
`status`, `assigned_to`, **and `archived`** — must equal the **live
thread-derived read** — specifically the **A1 tolerant readers**
(`get_current_status` / `get_current_assignment`, and the A1
`get_latest_archives` → `refs.archived` path, all as shipped in A1, unioning both
keyspaces), **not** the pre-Phase-3 slug-only readers and **not** the old
`folios.status` / `folios.archived` / stale `refs` cache. The A1 reader is the
right baseline because at A3 run-time a folio's latest control may be a
genesis-keyed row an A2 writer set in the A2→A3 window; the pre-tolerant
slug-only reader cannot see it and would report a false drift.

Mirror `skein/migrations/verify_versions_refs.py` into a thread-equivalence
checker that, for every folio in every db, compares the pre-migration live read
against the post-migration rebuilt `refs` value **for each of the three control
columns** — **values, normalized, not counts** (GitHub Scientist shadow pattern;
w3ix), including deleted-referent (orphan) semantics. `archived` carries 0 rows
today, so its equivalence is trivially green now — but it is proven, not assumed,
so that an archive write in the A2→A3 window or a faulty archive reduction is
caught before A5 drops `folios.archived` and removes the fallback. The fidelity baselines `threads-type-status`,
`stats-threads`, `stats-threads-orphan`, `stats-threads-weaver` stay green or are
deliberately rebaselined with written justification. The only intended moves in
3a are control-scoped and driven by the orphan drop (the 83 status orphans, the
1 orphan assignment, and the odd-shape assignment iff its disposition is drop —
no row collapse, that is 3b): every dropped row moves `stats-threads-orphan` and
the aggregate `stats-threads`; the 83 status orphans additionally move
`threads-type-status` (the assignment orphans do not). All move by **exactly the
logged drop count** (the count is the authority, not the prose estimate). Any `stats-threads` move larger than the
logged drop count is a real drift alarm, not an expected rebaseline.

---

## 6. Phase 3b — structural edges + the DAG (named, own gate)

3b gets its own design note and fell; sketched here only so 3a's boundary is
honest.

- Make `thread_hash` the **PK** (table rewrite); decide the `thread_id`
  column's fate. **In the same step:** the table-wide byte-duplicate collapse
  (all types, over post-transform bytes — including the control duplicates 3a
  left in place and any legacy non-control byte-dups), and the audit of every
  consumer that assumed a thread write yields a distinct row. These are 3b's
  because a `UNIQUE`/PK over the whole table is what forces the collapse, and 3a
  neither verifies non-control rows nor needs uniqueness.
- Add `within` / `published` to `ThreadType`.
- The **per-row endpoint audit**: for `reply` / `within` / `published` /
  `reference` / `mention` / `tag` / `succession`, classify each row B (both
  endpoints folios) or C (any non-folio endpoint) by §3.1. `reply` gets the same
  audit as its siblings — fmfs naming it "structural" is design intent, not a
  data guarantee, and live `reply` rows are largely not `F→F`.
- For confirmed Class B rows: decide head-vs-genesis anchor per type, re-anchor
  slugs → folio hashes.
- Class C stays slug/agent-keyed; its federation is a mesh-phase decision.
- Wire the existing RFC-6962 manifest to sign structural subgraphs (substrate
  built; the publish-path hookup, likely Phase 4), admitting **Class B
  structural rows only** — not merely "rows whose endpoints are hashes," since
  Class A genesis self-loops also have hash endpoints yet must stay local (§7).

---

## 7. Discipline (non-negotiables carried from fmfs)

- One running system, edited in place on legacy `skein/`. No parallel package;
  `skein_next/` is reference-only, never a runtime.
- Every commit additive + green; the API behaves identically until a capability
  is deliberately turned on. The CLI stays a pure client over the API.
- Risky store migrations proven on a **copy** first, then live with backups.
- Do **not** reimplement knurl or tweak the canon — `compute_thread_hash` uses
  the existing external contract unchanged.
- **The federating edge-hash set is Class B only.** Class B endpoints are
  folio/version hashes, so its `thread_hash` is a real edge-hash over endpoint
  folio hashes — the signed subgraph the mesh dedups/verifies/signs. Class A and
  Class C are **excluded from the Merkle manifest** for different reasons:
  - Class A is **local control, never federated** (§3.1), even though its rows
    are hash-keyed. Its `status`/`archive` self-loops anchor on a genesis hash
    and its `assignment` rows are `genesis → assignee` — and an assignee is an
    agent, not a folio hash, so Class A is not even uniformly "over endpoint
    folio hashes." It is hash-keyed for local row identity and dedup, full stop;
    a Phase-4 publish path must not admit it or it leaks per-station control
    state onto the wire.
  - Class C is hashed over local slugs/agent ids: content-addressed *row
    identity*, not a mesh-verifiable edge. Another station with the same content
    under a different local slug could not recompute the hash — the dual-ID leak.
  So the manifest admits Class B edges only; hash-keying a row never by itself
  makes it federation-eligible.
- Two-genotype fell (Claude + Codex/GPT, optionally a third genotype) on each
  load-bearing commit; the differential fidelity harness is the drift alarm.

---

## 8. Decided vs still open

**Decided (this gate + `24bk`):** state-first sequencing; one `thread_hash`
scheme (the existing `compute_thread_hash`) for all classes; **control class is
decided by type** (status/assignment/archive → A), **every non-control row's
class by its endpoints** (both-folio → B, any-non-folio → C, audited per-row);
only Class B federates; the end-state mixed keying; Class A = 3a, Class B/C
audit = 3b; genesis-keying for control with I1/I2 enforced; orphan =
drop-but-logged; non-self-loop status = normalize, agent to `weaver`; equivalence
target = live thread-derived read; deterministic `(created_at, thread_id)`
tiebreaker (same order column in reader and rebuild) as a two-typed-branch union
reduction; 3a adds `thread_hash` as a non-unique column only, deferring the
`UNIQUE`/PK, the table-wide byte-duplicate collapse, and the distinct-row
consumer audit to the 3b PK swap; the A4 all-dbs gate; the `folios.status` drop
collapses to a precondition guard and retires both `save_folio` and `move_folio`
writes.

**Open (resolve against real code, in 3b or its own brief):**

1. `thread_id` column's post-PK-swap fate (drop vs keep as audit handle).
2. Per-type head-vs-genesis anchor for `reply`/`within`/`published`.
3. The per-row B/C endpoint audit for every non-control type (`reply`,
   `reference`, `mention`, `tag`, `succession`) — no type is assumed folio↔folio.
4. Class C federation model (does an agent-graph edge ever travel, and how is an
   agent identified on the wire).
5. Any writer/consumer that assumed every thread write yields a distinct row,
   audited at the 3b PK swap when the table-wide byte-duplicate collapse and the
   `UNIQUE`/PK enforcement land together.

> Note on cited survey totals: `eq68` reports 12487 refs; `24bk` reports 12488.
> The one-row discrepancy between the two sources is immaterial to the design
> (neither figure gates a transform); the migration counts live state per-db at
> run time, not from either snapshot.
