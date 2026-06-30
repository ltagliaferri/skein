# Phase 3a test design

Status: test-design gate for Phase 3a (control taxonomy + `folios.status` drop).
Parent: `docs/PHASE_3_DESIGN.md` (the felled design gate). RSP phase 2 (test
design) — the oracle and properties are written **before** the A1–A5
implementation, red against today's unmigrated code, turning green as each step
lands. Research is `finding-20260630-eq68` / `-w3ix` / `-24bk`.

The job of this phase: build the executable spec that makes "additive + green,
legacy is the answer key" (fmfs non-negotiable #2) a thing a machine enforces,
not a thing we hope for.

---

## 1. The oracle — a differential equivalence verifier (the spine)

One artifact carries the correctness proof:
`skein/migrations/verify_threads_control.py`, a read-only sibling of the existing
`verify_versions_refs.py` (the Phase 1/2 B→C gate). Same posture: it does **not**
lean on "the harness is green" — it diffs the actual data, per db, `--all` /
`--sample`, and any divergence is a BLOCKER that exits non-zero.

**The independence rule (the load-bearing one).** The Phase 1/2 verifier never
trusts the production read path — it recomputes `compute_folio_hash` itself and
reads version fields directly. This oracle must hold the same line, and the first
test fell caught that the naive version does not: the parent design *mandates*
the A1 reader and the A3 rebuild share one reduction (same union, same
`(created_at, thread_id)` order). So `rebuilt_refs == A1_read` and
`read_pre == read_post` both validate the new reduction only against a **copy of
itself** — a shared reduction bug (reversed tiebreaker, wrong union column,
prefer-genesis-else-slug) passes every such leg green while every affected folio's
status is silently wrong. To break the circularity the oracle proves three
**independent** legs:

- **Leg A — transform/shadow.** Per-folio reduced control value is identical
  before and after migration. Covers the re-keying. Read-only oracles can't see a
  value that no longer exists, so the contract is explicit: A3 first writes a
  per-folio pre-image manifest (the reduced status/assigned_to/archived for every
  folio, by the pre-A3 live read) into the migration-mapping output; Leg A diffs
  the post-migration read against that frozen manifest. (Equivalently a
  copy/live pair — see copy-proof below.)
- **Leg B — rebuild mirror.** `rebuilt refs == A1 tolerant read` for all three
  control columns. Covers the cache rebuild *given* the reduction.
- **Leg C — independent reduction (the de-circularizing leg).** The mechanism
  must be concrete or it collapses back into circularity, so it is specified
  exactly. **Primary, the answer key:** an **independently-implemented reducer**
  — one hand-written function in the test corpus
  (`tests/fixtures/phase3a_expected.py`), sharing **no code** with the A1 reader
  or the A3 rebuild, **imported by both** the property tests (over fixtures) and
  the oracle (over real dbs). It is built and felled with the fixtures (§6 step
  2); the oracle imports it in step 3. The verifier calls it to run over the
  **frozen pre-A3 snapshot** (the slug-keyed data,
  captured before A3 mutates anything and digest-bound to the migrated copy per
  the copy-proof in §3) and emits an expected per-folio
  `status`/`assigned_to`/`archived` map. Post-A3 `rebuilt refs` is compared
  against **that frozen map**. Timing is the whole point: the legacy slug-only
  reduction only sees slug-keyed rows, so it is run against the **pre-A3
  snapshot**, never against the re-anchored data (running a slug reader post-A3
  reads nothing and defaults everything to open — that is the false-green path to
  avoid). A reduction bug shared by the A1 reader and the A3 rebuild is absent
  from this hand-written reducer, so Leg C catches it. **Secondary,
  corroboration only:** the per-folio fidelity baselines that pass through the
  thread reducer — `folios --status … --all` / `find-status-open` /
  `find-status-closed` (membership lists that *do* move when one folio flips
  open↔closed) — stay byte-green, captured pre-A3 per db and re-checked on the
  migrated copy. They are narrow (status-only, not every site) so they corroborate
  the primary reducer, they are not the answer key. Explicitly **excluded from
  Leg C**: `stats folios --by-status` (an aggregate histogram — compensating
  swaps across folios leave the counts identical, and it reads `refs.status`, the
  cache that *is* the A3 rebuild's output, so it is circular), and the
  thread-level baselines (`threads-type-status`, etc., which list raw threads, not
  the reduced value).

The structural invariants, also proven: `genesis_hash` unique per lineage (I1);
every control read resolves `folio → refs.genesis_hash → type-filtered threads`
(I2); no `threads.from_id` is NULL; every row has a non-null `thread_hash`.

**Cache-only state can be lost invisibly to Leg A/B, so it is gated as an A3
precondition, not an A5 guard.** A value present only in the `folios`/`refs`
cache with no covering thread (e.g. `folios.archived = 1` with no archive thread,
or a non-open `folios.status` with no status thread) is cleared by the A3
rebuild, and the A1 read also returns the default — so all three legs stay green
while state is silently lost across the A3→A5 window. `eq68` shows these are
empty ecosystem-wide today (`nonopen_no_thread = 0`, archived/assigned_to empty),
but the spec gates on it rather than assuming it: the cache-vs-thread guards run
as **A3 preconditions, before the rebuild** — refuse if any folio carries
cache-only control state with no covering thread.

The oracle is the load-bearing test. Wrong oracle = false green, which is worse
than no test — so it carries the heaviest fell (§5) and is built first.

**When the oracle runs.** It depends on the A1 reader and the `thread_hash`
column, so it cannot run before A1 exists. It runs from A1 onward: red (Leg
A/B/C diverge — data not yet re-anchored) from A1 through A2, green after A3. It
must **detect the pre-A1 schema and report a clean "not migrated" status, never
crash** on a missing column/reader. The pure-logic property tests (§3, over
fixtures with independently-computed expected values) are what run red from the
very start — those, not the oracle, carry the "test-first before any code" load.

---

## 2. The Fell-to-Fixture pattern (formalized here, pinned for the ecosystem)

Every confirmed finding from a fell becomes a permanent, named regression test,
so a defect the review caught can never silently return. This is a reusable
pattern; this doc is its first formal use.

Minimal structure (enough to be durable, not a framework):

- **Id.** Each regression test is tagged `fell:<artifact>:r<round>:<slug>` —
  e.g. `fell:phase3-design:r1:move-folio-after-drop`. The id links the test back
  to the exact fell round and finding that motivated it, so the provenance is
  recoverable from the test name alone.
- **Home.** Behavioral findings → a regression test in
  `tests/test_phase3a_threads.py` carrying the id in its docstring. Design-doc
  findings that have no runtime behavior (wording, dangling refs, survey-number
  consistency) → assertions in the doc-lint / not code tests; they are listed
  here for completeness but do not all map to executable tests.
- **Rule.** A fell does not close until every confirmed finding has either a
  regression test or a one-line note saying why it is doc-only.

The 18 findings from the Phase 3 design fell (7 rounds, opus + codex) map as
follows. Behavioral (→ regression test):

- `r1:union-reader` — tolerant reader reduces over the **union** of slug- and
  genesis-keyed rows, never prefer-genesis-else-slug.
- `r1:dedup-created-at` — two `closed` writes at different `now()` timestamps
  produce two distinct `thread_hash` values (re-assert is **not** idempotent).
- `r1:move-folio-after-drop` — `POST /folios/{id}/move` returns 200, not a
  `no such table: folios` 500, after the `folios` drop.
- `r1:all-insert-sites-hashed` — every insert site (the four in §2.4 of the
  design) stamps `thread_hash`; no new row lands NULL.
- `r1:assignment-orphan-oddshape` — the orphan and odd-shape assignment rows are
  handled, never blanket-re-keyed to `from_id = NULL`.
- `r1:a4-alldbs-gate` — the genesis-only reader is not reachable while any db
  still holds slug-keyed control threads.
- `r2:overlap-precedence` — a row that is both non-self-loop (`from = agent`)
  and orphan (deleted slug) is dropped, never normalized to NULL.
- `r2:two-typed-branches` — status reduces on `to_id`, assignment on `from_id`;
  neither reducer drops the other control kind.
- `r3:a2-window-passthrough` — a control row already genesis-keyed by an A2
  writer in the A2→A3 window passes through A3 untouched, not dropped.
- `r4:tiebreaker-determinism` — equal-`(folio, created_at)` groups resolve
  deterministically by `(created_at, thread_id)`, the same column in reader and
  rebuild.
- `r4:archive-path` — `get_latest_archives` feeds `refs.archived` and is not
  folded into the status reader.
- `r4:genesis-orphan-log` — a genesis-shaped endpoint with no matching
  `refs.genesis_hash` is dropped and logged as a genesis orphan.
- `r6:archive-equivalence` — the oracle proves `refs.archived`, not just
  status/assigned_to.

Doc-only (no runtime behavior; recorded, not code-tested): the §7 Class-A
federation scoping, the §3.1 header wording, the dangling §3.2/§9.3 refs, the
survey 12487/12488 note, the "zero callers scoped to skein/" precision, the
refs-provenance ground-truth correction. These were design-doc fixes; the fell
that produced them is closed by the committed `PHASE_3_DESIGN.md`.

**Pin:** the ecosystem-wide formalization of this pattern (a shared id scheme, a
cross-project registry, tooling to assert "every fell finding has a test") is
filed as a SKEIN notion for later — out of scope for 3a, which just uses the
pattern once, here.

---

## 3. Coverage — properties by step

The property tests (pure logic over fixtures, with **independently-computed
expected values**) are red from the start. The oracle legs go red→green from A1
(§1, "when the oracle runs"). Grouped by A1–A5 plus cross-cutting.

A1 (tolerant reader + new type + non-unique column):

- reader returns the identical value across three input shapes — all-slug-keyed,
  all-genesis-keyed, split across both — for status, assignment, archive. The
  expected value is **computed independently in the test** (a hand-written
  reducer or a literal), never asserted as `reader == rebuild` (that is the
  circular check §1 forbids).
- deterministic tiebreaker on equal-timestamp groups
  (`r4:tiebreaker-determinism`): the test names the **expected winning row**
  (the one the design rule must pick), not merely "reader and rebuild agree."
- the `thread_hash` column is added nullable and **non-unique** (no UNIQUE index
  in 3a — that is 3b).

A2 (genesis-keyed writers + thread_hash everywhere):

- a new control write lands genesis-keyed.
- all four insert sites stamp `thread_hash` (`r1:all-insert-sites-hashed`).
- the backfill repair-mode mint either stamps `thread_hash` or is provably
  frozen (the design's escape clause).
- a re-asserted identical state at a new `now()` produces a **distinct**
  `thread_hash` (`r1:dedup-created-at` — re-assert is not idempotent).

A3 (the migration):

- classify-by-shape: already-genesis pass-through (`r3:a2-window-passthrough`);
  slug resolvable → re-key (status/archive self-loop; assignment `from = genesis`,
  `to` untouched, plus the plain resolvable assignment happy path); non-self-loop
  → normalize, agent to `weaver`; unresolvable → drop-but-logged with the right
  orphan category — both the slug orphan and the **genesis orphan**
  (`r4:genesis-orphan-log`).
- overlap precedence (`r2:overlap-precedence`); never writes `from_id = NULL`.
- **A3 preconditions** (refuse, before the rebuild): I1 genesis-uniqueness; and
  the cache-vs-thread guards (§1) — no folio with cache-only `status` /
  `assigned_to` / `archived` lacking a covering thread.
- **idempotent**: re-running A3 on already-migrated data is a no-op (same rows,
  same hashes, empty audit delta).
- **audit-log correctness, not just presence**: every drop/normalize entry names
  the original row identity, its old endpoints, the new endpoints (or the drop
  reason + orphan category), the type, and the disposition; and the entry counts
  **reconcile** to the actual row deltas and to the fidelity-baseline movement.
- the oracle's three legs hold post-A3 (§1).

A4 (reader genesis-only): the all-dbs gate (`r1:a4-alldbs-gate`); equivalence
still exact once the gate holds.

A5 (drop `folios.status` + the table): the precondition guard (nothing lives
only in `folios.status`; assigned_to/archived/target_agent/omlet/
acknowledged_at/metadata empty or mirrored in refs); **every `folios`-touching
consumer survives the drop** — not just `move_folio` and `save_folio`
(`r1:move-folio-after-drop`), but the list/enrich read paths (the
`folio.status` fallback at `utils.py:108`), `migrate_folios_from_json` (gated or
removed), and the removal of the dead `FROM folios` fallbacks. A smoke per path
that it returns 200 / its value, not a `no such table: folios` 500.

Cross-cutting:

- **Copy-proof, with a machine check tying live to the proven copy.** The
  migration runs on a copy first and the oracle is green on the copy before any
  live `--all --backup` run — and a digest check confirms the live db's
  pre-migration preimage (row/content digest of the control-thread set) matches
  the copy that was proven, so the proof can't pass on one preimage and the live
  run execute on a different one (different data, code version, or flags).
- the existing fidelity baselines (`threads-type-status`, `stats-threads`,
  `stats-threads-orphan`, `stats-threads-weaver`) stay green or move by exactly
  the logged drop count (the only intended 3a moves; §5.1 of the design).

---

## 4. Fixtures — synthetic, full-real, and a shape-preserving sample

Three sources, each for a different job:

- **Synthetic generator** (`tests/fixtures/phase3a_synth.py`): builds a small db
  with **one row per A3 transform branch**, deterministically — the coverage
  contract is "every branch the A3 classifier can take has a fixture." That set:
  plain status self-loop (slug, resolvable) → re-key; 39-style non-self-loop
  (`from = agent`, resolvable) → normalize; 83-style **slug orphan** (slug, no
  ref) → drop; **genesis orphan** (genesis-shaped endpoint matching no live
  `refs.genesis_hash`) → drop, distinct log category; **already-genesis
  passthrough** (A2-window row) → untouched; overlap row (`from = agent` **and**
  deleted slug) → drop, not normalize; equal-timestamp tie group; byte-identical
  duplicate pair; multi-status-history folio; mixed-keyspace folio (slug + genesis
  for one folio); **plain resolvable assignment** (`from = folio` → genesis, `to`
  kept) — the happy path; orphan assignment → drop; odd-shape assignment;
  constructed archive self-loop; and an **I1 genesis-collision** db (two lineages
  sharing a genesis) to drive the precondition refusal. Fast, hermetic — the home
  of the property and Fell-to-Fixture tests. The **independent expected-value
  reducer** (§1 Leg C) is a sibling module (`tests/fixtures/phase3a_expected.py`)
  built and felled alongside these fixtures, then imported by the oracle — one
  reducer, two callers. Content is synthetic, so this generator tests migration
  **logic and shapes**, not real-hash identity.

- **Full real dbs** (this phase): the differential oracle runs `--all` over the
  real 45 registered dbs on a **copy** (copy-proof), proving real-**hash**
  identity and real **breadth**. Honest scope: a read-only sweep of the live 45
  shows several adversarial shapes are **0 today** (overlap-orphan, equal-timestamp
  ties, archive rows), with only the odd-shape assignment (1) and a byte-identical
  duplicate group (1) actually present. So the full-real run is the hash-identity
  + breadth gate; the adversarial branches are covered by the **synthetic
  generator** (constructed), not by current real data. Both are needed for the
  claim to hold. The real shit is the real shit — but it does not contain every
  shape, so we construct the rest.

- **Shape-preserving real sample** (going forward, the saner alternative to a
  date-cut): a deterministic sampler (`skein/migrations/sample_shapes.py`,
  read-only) that selects from a real db the minimal set of folios/threads
  covering every shape class above, plus a small per-type breadth sample, and
  freezes it as a checked-in fixture. Curated **by shape**, not by date, so it
  does not drift the way "old data" would, and it is reproducible from any real
  db. Redaction caveat: the A3 classifier branches on whether a control endpoint
  matches a live `refs.genesis_hash`, and redacting content moves the genesis
  hash — so the sampler must redact **only non-hashed fields**, or consistently
  rewrite every genesis-keyed endpoint to the recomputed genesis. Otherwise a
  genesis-passthrough row silently becomes a spurious genesis-orphan and the
  fixture exercises the wrong branch. With that caveat it is a logic/shape fixture
  (the full-real run owns hash-identity), built once 3a's shapes settle; the
  permanent regression fixture for 3b and beyond.

---

## 5. Fell policy for the test artifacts (round-capped)

The design gate earned an uncapped fell (7 rounds) because it is the
architecture. The test code is more mechanical, and we are going hard already, so
the test artifacts are round-capped:

- **The oracle** (`verify_threads_control.py`): two-genotype (opus + codex),
  **2-round cap**. It is where wrong = false green.
- **Property tests + fixtures + the synthetic generator**: two-genotype,
  **2-round cap** (raised from 1 after the first test-design fell). These now
  carry the §1 Leg C independent expected-value reducer and the per-branch
  fixture set — i.e. the de-circularizing logic the oracle leans on — so they are
  load-bearing, not self-evident, and a 1-round pass already missed branches.

Escape clause: a **genuine blocker** at the cap overrides the cap — we do not
ship a known-broken oracle to honor a round count. The cap bounds
diminishing-returns nitpicking, not real defects. A finding that survives the cap
as "real but deferred" is filed, not silently dropped.

---

## 6. Sequencing

1. Write this doc; fell it (2-round cap, two-genotype). ← gate
2. Build the **synthetic generator + per-branch fixtures + the independent
   expected-value reducer (Leg C) + property tests + the Fell-to-Fixture
   corpus**, red from the start (pure logic, no migration code needed); fell
   (2-round cap). This is first because it is what runs red before any
   implementation exists and because it holds the de-circularizing logic.
3. Build the oracle (`verify_threads_control.py`); fell it (2-round cap). It
   needs the A1 reader + `thread_hash` column, so it is exercised against the
   fixtures now (Leg C, structural checks) and goes live against real dbs from
   A1 onward — reporting a clean "not migrated" status, never crashing, on pre-A1
   schema.
4. Then — and only then — A1 implementation begins, turning the red tests green
   step by step, each commit two-genotype-felled and harness-green.

The mechanical parts (the synthetic generator, the property-test scaffolding)
can be built in parallel via spindle; the oracle and the Leg C reducer stay in
the implementer's own hands.
