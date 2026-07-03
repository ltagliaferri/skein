# Phase 3b design gate — thread_hash as PK + the structural (Class B) DAG

Sibling of `PHASE_3_DESIGN.md` (which sketched 3b in §6 and named the open
questions in §8). This is 3b's own gate: the load-bearing decisions, grounded in
a first-hand audit of current `master`, with a fell before any implementation.

Phase 3a is done: control taxonomy landed in threads/refs, `folios` dropped
ecosystem-wide 2026-07-03 (`finding-20260703-a6kv`). 3b is the **structural
half** — it makes the busiest table content-addressed by identity (the PK swap),
re-anchors the folio↔folio edges onto content hashes (Class B), and leaves the
Merkle-manifest hookup as the only forward-looking stub (Phase 4).

---

## 1. What 3b is, and its boundary

Three coupled moves, in dependency order:

1. **The PK swap** — `thread_hash` becomes the primary key (a table rewrite),
   enforcing table-wide uniqueness. This forces the byte-duplicate collapse and
   the distinct-row-consumer audit that 3a deliberately deferred (`PHASE_3_DESIGN`
   §4). The random `thread_id` **stays as a non-identity audit handle** (§4).
2. **The Class B re-anchor** — every non-control row whose *both* endpoints
   resolve to a folio is re-keyed from slugs to folio/version hashes and its
   `thread_hash` recomputed. This is a per-row audit (§5), not a per-type rule.
3. **Manifest admission** — the existing RFC-6962 manifest (`skein/canon.py`,
   already built and kind-agnostic) admits **Class B rows only**. The publish-path
   hookup itself is Phase 4; 3b only fixes which rows are *eligible*.

**Out of scope (stated so the boundary is honest):** the live publish/federation
path (Phase 4); any change to Class A control (done in 3a) beyond the mechanical
PK swap; Class C endpoint re-anchoring (a mesh-phase decision, §7).

**Discipline (carried from `PHASE_3_DESIGN` §7, non-negotiable):** one running
system edited in place on `skein/`; every commit additive + green with the API
behaving identically until a capability is deliberately turned on; risky store
migrations proven on a copy first, then live with backups; `compute_thread_hash`
used unchanged (no knurl/canon edits); two-genotype fell on each load-bearing
commit + the differential fidelity harness as the drift alarm.

---

## 2. Ground truth — re-baselined first-hand (all 47 registered dbs, `master @ 34d8437`)

`PHASE_3_DESIGN` §2 was verified against `276796f`, before A2–A5. The data has
moved; these are the numbers 3b actually operates on.

### 2.1 The table today

```
threads(
  thread_id   TEXT PRIMARY KEY,   -- random 'thread-YYYYMMDD-xxxx'
  from_id     TEXT NOT NULL,      -- slug | agent id | (supersedes/reverted) version hash
  to_id       TEXT NOT NULL,
  type        TEXT NOT NULL,
  content     TEXT,
  weaver      TEXT,
  created_at  DATETIME NOT NULL,
  thread_hash TEXT                 -- A1 added it nullable+non-unique; A2 populates it
)
```

- **`thread_hash` is 100% populated — zero NULL rows across all 47 dbs.** The A2
  write-path stamping is complete ecosystem-wide, so the PK swap promotes a full,
  already-verified column. This is the single biggest de-risk: 3b is a *rename of
  the identity*, not a backfill.
- **Byte-duplicate rows: exactly 1 group ecosystem-wide** (rows identical on all
  six canonical fields, differing only by the random `thread_id`). The "table-wide
  collapse" §4 of the parent doc anticipated is effectively empty — one group
  collapses; everything else is already unique by content.
- Type distribution (all dbs, 10,328 rows): `status` 5283, `message` 2417,
  `tag` 1435, `reference` 713, `reply` 240, `mention` 185, `succession` 51,
  `supersedes` 2, `assignment` 2. (`archive`, `within`, `published`, `reverted`:
  0 rows.)

### 2.2 The five insert paths (all already compute `thread_hash`)

The PK swap changes the *collision semantics* of every insert, so all five are
audited:

1. `save_thread` (`storage.py:1019`) — `INSERT OR REPLACE`. All control sugar
   routes + the generic `POST /threads`. Computes `thread_hash` over the
   post-genesis-keyed endpoints.
2. `_maintain_versions_refs` **reverted** mint (`storage.py:1483`) — plain
   `INSERT`, "one marker per revert" (deliberately un-deduped).
3. `_maintain_versions_refs` **supersedes** mint (`storage.py:1504`) — plain
   `INSERT`, guarded by an endpoint dup-check (`storage.py:1498`).
4. `backfill_versions_refs` repair mint (`:297`) — spent as a live migration but
   still run by `fidelity/harness.py` on the folios-bearing frozen fixture.
5. `migrate_threads_from_json` (`storage.py:1933`) — `INSERT OR IGNORE`, frozen
   importer (no-ops once the table is non-empty).

### 2.3 The `thread_id` consumers (the constraint on dropping it)

- **Reader tiebreaker (load-bearing):** `_latest_control_by_folio`
  (`storage.py:1267`) reduces control with `ORDER BY t.created_at DESC,
  t.thread_id DESC`. `get_latest_statuses/assignments/archives` and the
  `utils.py` `get_current_*` all funnel through it — one tiebreaker site.
- **The equivalence oracle** (`verify_threads_control.py`) hard-codes the same
  `(created_at, thread_id)` order in its independence rule; reader and rebuild
  must share it.
- **API surface:** `POST /threads` returns `{"thread_id": ...}`
  (`routes.py:1163`). **No endpoint looks a thread up by id** — there is no
  `GET /threads/{id}`, no `WHERE thread_id=` in any client path. The returned id
  is informational, not a handle.
- **One-shot migration** `migrate_threads_control.py` uses `WHERE thread_id=?` to
  target rows — spent after A3, not a steady-state consumer.
- The `Thread` model (`models.py:176`) requires `thread_id: str`.

No consumer counts threads as a proxy for logical events; the two `COUNT(*) FROM
threads` sites are a NULL-check (verify) and an idempotency gate (migrate),
neither distinct-row-sensitive.

---

## 3. The end-state schema

```
threads(
  thread_hash  TEXT PRIMARY KEY,   -- compute_thread_hash(...); the row's identity
  thread_id    TEXT NOT NULL,      -- KEPT: random audit handle + reader tiebreaker (§4)
  from_id      TEXT NOT NULL,      -- Class A: genesis hash; B: folio/version hash; C: slug/agent
  to_id        TEXT NOT NULL,
  type         TEXT NOT NULL,
  content      TEXT,
  weaver       TEXT,
  created_at   DATETIME NOT NULL
)
```

Indexes carried unchanged (`from_id`, `to_id`, `type`, `created_at`, and the two
compound control indexes). The end state stays an **intentionally mixed-keyed
table** (`PHASE_3_DESIGN` §3.1): Class A on genesis hashes, Class B on
folio/version hashes, Class C on slugs/agents — every row content-addressed by
`thread_hash`, endpoints keyed by what the edge actually connects.

---

## 4. The PK swap — decisions

### 4.1 Dedup semantics (unchanged from `PHASE_3_DESIGN` §4, restated for the swap)

`thread_hash` hashes all six canonical fields **including the normalized
`created_at`**. So the PK collapses only **fully byte-identical** rows. It does
**not** make "re-assert the same logical state" idempotent — live writers stamp
`created_at = now()`, so setting a status to `closed` twice yields two rows, two
hashes, both kept (the status audit trail). Only genuine byte-duplicates collapse:
today, one group ecosystem-wide.

### 4.2 `thread_id`: KEEP as a non-identity audit handle

The parent doc left this open (§8 #1). The audit resolves it toward **keep**:

- It is the **reader tiebreaker** (`storage.py:1267`) and the oracle's order
  column. Dropping it forces the tiebreaker onto `thread_hash` in *both* the
  reader and the verifier — and `thread_hash DESC` picks a **different** winner
  than `thread_id DESC` on an equal-`created_at` tie. There are 0 such ties today
  (latent, not live), so it changes nothing observable now, but it is a real
  reduction change with no functional payoff (`thread_hash` is no more meaningful
  a tiebreaker than a random id).
- It is in the `POST /threads` response and the `Thread` model.

Keeping it means **only the PK/uniqueness moves** — smallest blast radius,
additive, green; the tiebreaker and the oracle stay byte-identical. Dropping
`thread_id` is a valid *later* hygiene pass once it is proven dead, not part of
the load-bearing swap. **Decision: keep `thread_id` (NOT NULL, non-PK) in 3b.**

### 4.3 Insert paths → uniform `INSERT OR IGNORE` on the `thread_hash` PK

Post-swap, all steady-state inserts become `INSERT OR IGNORE` on the `thread_hash`
PK — uniform, idempotent, content-addressed (matching what `migrate_threads_from_json`
already does):

- `save_thread`: `OR REPLACE` → `OR IGNORE`. On a byte-identical re-save it keeps
  the **original** row (and its `thread_id` audit handle) rather than churning it.
  Correct for control too: two identical status writes in the same normalized
  instant are the same logical event; different instants → different hash → both
  kept.
- The **reverted** mint (`:1483`, plain `INSERT`) → `OR IGNORE`. Today it is
  `PHASE_3_DESIGN`'s one deliberate distinct-row assumption ("one marker per
  revert"). It is preserved: distinct reverts carry distinct `created_at` →
  distinct `thread_hash` → distinct rows. Only a physically-impossible
  same-normalized-instant double-revert would collapse — acceptable, and `OR
  IGNORE` keeps the swap from raising a PK violation on it.
- The **supersedes** mint (`:1504`) already endpoint-dup-guards; add `OR IGNORE`
  as the belt-and-suspenders on the PK.

This is the distinct-row-consumer audit (§8 #5) resolved: the reverted marker is
the only intentional per-event row, and it survives the swap.

### 4.4 The migration shape (SQLite can't ALTER a PK)

Per-db, fail-closed, on the `cutover_threads_control.py` / `drop_folios.py`
template (backup → transform on a copy-proof → live with the pre-image as the
restore point):

1. **Backup** `<db>.bak-threads-pk-<stamp>` (online-backup API, WAL-consistent);
   refuse if a prior backup exists.
2. Build `threads_new` with the §3 schema (`thread_hash` PK).
3. **Re-anchor Class B rows first** (§5) — resolve endpoint slugs → hashes and
   **recompute `thread_hash`** (endpoints are hashed, so re-anchoring changes the
   identity). A re-anchored row may now collide with an existing row → it collapses
   under the PK (logged).
4. Copy every row into `threads_new` with `INSERT OR IGNORE` (the collapse). Log
   each dropped `thread_hash` with its reason (byte-dup vs re-anchor-collision).
5. `DROP threads; ALTER threads_new RENAME TO threads`; rebuild indexes;
   `wal_checkpoint(TRUNCATE)`.
6. **Verify** (§6) or abort the whole run (already-migrated dbs restore from
   `.bak`).

---

## 5. The per-row B/C endpoint audit (the load-bearing classification)

`PHASE_3_DESIGN` §3.1: control class is by type (Class A, done in 3a); **every
other row's class is decided by its endpoints, per row**. The rule at migration
time: a non-control row is **Class B** iff *both* endpoints resolve to a folio (a
live `refs.slug` or a `versions.content_hash`); otherwise **Class C**.

### 5.1 The live distribution (F→F share = Class B candidates)

- `reference` — 706 F→F / 7 not — **99% B**
- `mention` — 145 / 40 — **78% B**
- `succession` — 13 / 38 — **25% B** (rest agent→agent C)
- `reply` — 5 / 235 — **2% B**
- `message` — 54 / 2363 — **2% B**
- `tag` — 15 / 1420 — **1% B** (but see 5.2)

This is the empirical proof of the per-row thesis: no type is uniformly one class.
`reply` at 2% F→F confirms `PHASE_3_DESIGN` §3.1's warning — fmfs naming reply
"structural" is intent, not a data guarantee.

### 5.2 Edge cases the naive endpoint test gets wrong (found by sampling)

The raw "both endpoints resolve to a folio" test is necessary but not sufficient;
the impl audit applies these overrides:

- **`tag` self-loops (the 15 "F→F"):** every one is `folio → same folio`
  (`issue-…-ojlo → issue-…-ojlo`). A tag is semantically `folio → label token`;
  the label coinciding with the slug does not make it a structural folio↔folio
  edge. **Override: `tag` is Class C** (or a degenerate self-loop to drop-log);
  it does not federate.
- **`reference` non-F→F (the 7):** dangling/non-folio endpoints — site names
  (`skein-development`), deleted folios (`summary-…-xxxx`), test fixtures
  (`issue-1 → summary-123`). These are **orphans**: an endpoint slug that resolves
  to no folio.
- **`mention` non-F→F:** `folio → agent` / `folio → topic token`
  (`brief → agent-456`, `→ ai-sdk`). Genuine Class C.
- **`succession` F→F:** genuine `folio → folio` chains
  (`brief → brief`, `finding → finding`) — Class B; agent→agent successions are
  the fmfs agent-lifecycle Class C. Succession is the cleanest per-row split.

### 5.3 Orphan handling — keep + log, do NOT drop

3a dropped control orphans because control is **regenerable from threads** (the
answer key survives). Structural edges are **not regenerable** — an orphaned
`reference` is the only record of that pointer. **Decision: an unresolvable
endpoint means the row is NOT re-anchored — it stays as-is (Class C, slug-keyed)
and is logged; never silently dropped.** (Contrast 3a's drop-but-logged control
orphans.) A later pass may prune truly dead references, but not inside the
load-bearing swap.

---

## 6. Head-vs-genesis anchor per Class B type

An edge anchored on a **genesis** hash is lineage-scoped (survives edits to the
target); on a **head/version** hash it is version-scoped (pins an exact version).
The decision per confirmed-B type:

- `supersedes` / `reverted` — **version (head)**, already shipped: they pin exact
  versions by definition.
- `reference` / `mention` / `reply` / `within` — **genesis (lineage)**. You
  reference/mention/reply-to *the folio*, not a frozen version; a head anchor
  would dangle the moment the target is edited.
- `succession` (F→F subset) — **genesis (lineage)**: a supersession chain between
  distinct lineages tracks the lineages.
- `published` — **version (head)**: publishing pins the exact version put on the
  wire (design-forward; 0 rows today).

Default is **genesis/lineage**; the version-pinning exceptions are the edit-DAG
edges and `published`. The per-row eyeball of the residual edge rows (§5.2) is an
implementation precondition, not re-litigated here.

---

## 7. Manifest admission — Class B only

`skein/canon.py` already builds an RFC-6962 Merkle manifest over folio **and**
thread content hashes; the substrate is kind-agnostic and in place. 3b's only job
here is **eligibility**: the manifest admits **Class B rows only**.

- **Class A excluded** — local control, never federated, even though its rows are
  hash-keyed; its `assignment` endpoints are agents, so it is not even uniformly
  "over endpoint folio hashes." Admitting it leaks per-station control onto the
  wire.
- **Class C excluded** — hashed over local slugs/agent ids: content-addressed row
  *identity*, not a mesh-verifiable edge. Another station with the same content
  under a different local slug could not recompute the hash (the dual-ID leak fmfs
  warns against).

Hash-keying a row never by itself makes it federation-eligible. The **live publish
hookup is Phase 4**; 3b fixes the admission predicate and leaves the wiring
stubbed. **Class C federation (does an agent-graph edge ever travel, and how is an
agent named on the wire) is an open mesh-phase question (§9 #3), not resolved here.**

---

## 8. Test design (RSP posture — written before implementation)

The migration is load-bearing; the verifier is the spine, mirroring
`verify_threads_control.py` (the 3a control oracle) and `verify_versions_refs.py`.
Read-only, per-db, `--all`/`--sample`, every divergence a BLOCKER, never mutates an
input (runs over a private WAL-consistent copy), copy-proof digest binding.

**Equivalence target (the shadow read):** for every folio in every db, the
**live control read is unchanged** across the swap — `get_latest_statuses /
assignments / archives` return identical values before vs after (the reduction and
its tiebreaker are untouched because `thread_id` is kept). Values, normalized, not
counts.

**Structural post-conditions:**
- `thread_hash` is the PK and is UNIQUE (the swap held).
- Row count = pre-count − (logged byte-dup collapses + logged re-anchor
  collisions), to the exact logged count; any larger loss is a drift alarm.
- Every Class B row's endpoints resolve to `versions.content_hash`
  (re-anchor succeeded); every Class C row's endpoints are unchanged.
- No `thread_hash` NULL; no `from_id`/`to_id` NULL; `thread_id` NOT NULL preserved.
- `thread_hash` self-verifies: `compute_thread_hash(row)` equals the stored PK for
  every row (catches a re-anchor that changed endpoints but not the hash).

**Fidelity harness:** the `threads-*` baselines stay green or are deliberately
rebaselined with written justification tied to the logged collapse/re-anchor set
(the count is the authority, not prose). The differential harness is the drift
alarm on every commit.

**Positive-path tests (the lesson from the A5 cleanup fell):** inject each failure
and assert the verifier BLOCKS — a re-anchor that leaves a dangling endpoint, a
`thread_hash` that no longer self-verifies after re-anchor, a row count off by more
than the logged collapse, a surviving duplicate `thread_hash`. Clean-path tests
alone do not prove a gate fires.

---

## 9. Decided vs open

**Decided (this gate):**
1. `thread_id` **kept** as a NOT-NULL non-PK audit handle + reader tiebreaker
   (§4.2); dropping it is a later hygiene pass, not the swap.
2. All steady-state inserts → `INSERT OR IGNORE` on the `thread_hash` PK (§4.3);
   the reverted marker's per-event intent survives.
3. Migration is a per-db table rewrite on the A3/drop-folios template, re-anchor
   Class B **before** the copy and recompute `thread_hash`, collapse via
   `OR IGNORE` with every dropped hash logged (§4.4).
4. B/C is decided per row by endpoint resolution, with type-semantic overrides:
   `tag` self-loops are C, orphans are C-and-logged-never-dropped (§5).
5. Class B anchors: genesis/lineage by default; version/head for
   supersedes/reverted/published (§6).
6. The manifest admits Class B only; the publish hookup is Phase 4 (§7).

**Open (resolve in implementation or a follow-up brief):**
1. The full per-row eyeball of the residual edge rows (§5.2) — the 7 reference
   orphans, the 15 tag self-loops, the `reference`/`mention` F→F tails — confirmed
   against live data at migration time, not assumed from the sample.
2. `within` / `published` — added to `ThreadType` now (0 rows), but their write
   paths and exact anchor land when a feature uses them.
3. Class C federation model (mesh phase): does an agent-graph edge ever travel,
   and how is an agent identified on the wire.
4. Whether the residual dead references (orphans kept in §5.3) ever get a separate
   pruning pass.

> Note on counts: every figure here is a live per-db measurement at audit time
> (`master @ 34d8437`, 47 dbs, 10,328 thread rows), not a frozen snapshot; the
> migration re-measures per-db at run time and the logged counts — never this
> prose — are the transform's authority.
