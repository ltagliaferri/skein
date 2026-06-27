# Phase 0 research note — correct the folio content hash

Status: for review before any `storage.py` edit. Grounded in the live tree and
the `cutover-20260624` reference branch as they stand, not the briefs' summaries.

Parent: brief-20260626-fmfs. Phase 0 brief: brief-20260626-48bj.

## What Phase 0 is

Make every folio carry its true `sha256::` content hash, computed from the
five canonical fields with `created_at` normalized, recomputed on every write.
Same schema, same tables, no route changes, nothing resolves by hash. The only
stored data that changes is the `content_hash` column's values.

## The one-line problem in the live code

`skein/storage.py:40` `compute_folio_hash(folio)`:

```python
immutable = {... "created_at": folio.created_at.isoformat() if folio.created_at else None ...}
canonical = canon.serialize(immutable)
return knurl_hash.compute(canonical.decode("utf-8"), prefix="folio")   # -> "folio:sha256:<hex>"
```

And `save_folio` (storage.py:1509) computes it ONCE: `if not folio.content_hash`.
So an edited folio keeps its first hash forever — that compute-once guard, not
raw-string hashing, is why ~26% of stored digests don't reproduce (verified:
7,607/10,254 reproduce, 2,647 don't, 2,085 missing — across 41 DBs).

Two value changes result from Phase 0:
- Framing: `folio:sha256:` -> `sha256::` (every row).
- Digest: changes for any row whose stored hash is stale (edited after first
  compute, or computed before `created_at` normalization existed). The live read
  path already normalizes `created_at`, so legacy-via-read equals new-canon for
  100% of folios — the staleness is the guard, not the math.

## Decision 1 — scope the port to the folio-identity slice only

The reference `skein/canon.py` carries far more than folio identity: thread
canon, the RFC-6962 Merkle manifest, redeem-challenge canon, signing seams.
Those belong to Phase 3 (threads) and Phase 4 (mesh). Porting the whole module
now drags mesh machinery into Phase 0 and creates surface we don't use.

Port only: `normalize_created_at`, `_parse_timestamp`, `_FRACTION_RE`,
`_require_str_or_none`, `CANONICAL_FIELDS`, `folio_canonical_fields`,
`folio_canonical_bytes` (canon), and `content_hash_for_bytes`,
`compute_folio_hash(fields)` (identity). Leave thread/merkle/redeem for their
phases. The slice is closed under its own imports (knurl only).

## Decision 2 — module layout, and the no-dual-path rule

Create `skein/canon.py` (the folio canonical-bytes producer) and
`skein/identity.py` (frames the digest as `sha256::`). These are NOT yet live
(A0 absorbed signing/address/words, not canon), so adding them is additive.

The dual-path trap is two folio-hash implementations. Resolution:
- `skein/storage.py:compute_folio_hash(folio)` stops computing anything itself.
  It extracts the five fields from the `Folio` and DELEGATES to
  `identity.compute_folio_hash(fields)`. The inline `canon.serialize(...) /
  knurl_hash.compute(prefix="folio")` body is deleted.
- One folio-hash function exists after this: `identity.compute_folio_hash`.
  `signing.py` does not hash folios (verified), so there is no third path.

`normalize_created_at` accepts a `datetime` directly, so the storage wrapper
passes `folio.created_at` (a datetime) straight through — no isoformat round-trip.

## Decision 3 — recompute at the single write chokepoint

Every external write (routes.py:705, 928, 1954) goes through
`StorageManager.save_folio` -> `LogDatabase.save_folio` (storage.py:1024), the
lowest DB writer. Put the recompute THERE, unconditionally:

```python
# LogDatabase.save_folio, before the INSERT/UPSERT
if KNURL_AVAILABLE:
    folio.content_hash = compute_folio_hash(folio)
```

Rationale: a hash computed in the `StorageManager` wrapper can be bypassed by any
direct `_log_db.save_folio` call; computed in `LogDatabase.save_folio` it cannot.
One write path, always correct. Remove the wrapper's `if not folio.content_hash`
compute and the `get_folio` write-on-read recompute (storage.py:1521-1523) — once
every write recomputes and the backfill has run, read-time mutation is dead weight
and a surprise write under a read.

Consequence to state plainly: between Phase 0 and Phase 2, `content_hash` is
correct-for-current-content but MUTABLE (edit still mutates in place until
edit-becomes-commit). Acceptable only because nothing resolves by hash yet.

## Decision 4 — the migration (an identity migration, copy-first)

New `skein/migrations/backfill_content_hash.py`, modeled on
`normalize_datetimes.py` (registry-driven, idempotent, `--dry-run`, per-db).

1. Corpus stats on a COPY first. For each of the ~40 registered DBs, copy it,
   recompute every folio's hash, and count: reframe-only vs digest-changed vs
   was-missing. Emit the blast radius before any live write.
2. Old->new mapping. While migrating, write `{db, folio_id, old_hash, new_hash,
   change_kind}` to a log (JSONL per project under a migration-output dir). This
   is rollback evidence and lets Phase 1 optionally resolve stale
   `folio:sha256:` refs as aliases.
3. Idempotent. Re-running recomputes to the same `sha256::` value; a second run
   reports zero changes.
4. FTS triggers. `content_hash` is not an FTS column, but the UPDATE must not
   disturb the FTS5 triggers/shadow tables. Verify trigger set is intact
   before/after on the copy.
5. WAL + concurrency. Live DBs are WAL; agents may be writing. Per project:
   checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`), take the backup, then run the
   backfill UPDATE inside one `BEGIN IMMEDIATE` transaction with a
   `PRAGMA busy_timeout` set, so a concurrent writer waits rather than tears.
   For the actively-used DBs (this skein project included), quiesce the server
   for the window rather than racing it. Backups cover rollback, not a torn run.

Open sub-question for you: do we quiesce all 40 at once in a maintenance window,
or roll project-by-project? I lean project-by-project, idle ones unattended,
busy ones (skein, speakbot) quiesced deliberately.

## Decision 5 — the two gates

- Hash correctness: port `skein/tests/test_canon_conformance.py` from the branch
  into the live tree. Its `VECTORS` are self-contained (frozen `canonical_bytes`
  + `content_hash` for nfc/nfd collapse, `Z`/offset -> UTC, fractional-second
  truncation, whole-second). Add the cross-check: N real live folios hash equal
  to the reference `identity.compute_folio_hash`. This is the gate the fidelity
  harness structurally cannot be (it blanks `content_hash`).
- Behavioral invisibility: `fidelity/harness.py check` stays green across the
  whole Phase 0 change (it normalizes `content_hash` out, so a correct Phase 0
  shows zero stable drift). Baseline already blessed on clean master.

## What this phase explicitly does NOT do

No schema change, no addressing by hash, no thread/mesh canon, no immutability.
Edit still mutates in place. Slug still addresses everything. The three-table
split, status-column drop, and edit-as-commit are Phases 1-3.

## RSP framing (Research -> Test Design -> Implementation -> Hardening)

Phase 0 qualifies as a Rock-Solid Primitive: isolatable (the folio-hash
function, knurl-only), load-bearing (the ecosystem's `sha256::` contract), and
with a nameable boundary (five fields in, one framed digest out, defined error
cases). So it runs the full four phases, no skipping
(playbook speakbot:playbook-20251218-pb2o).

**Phase A — Research (this note).** The problem is the compute-once guard, the
fix is the ported normalized canon, the blast radius is ~26% of digests + all
framing, the dual-path risk is two hash functions, the migration risk is WAL
concurrency. In review now.

**Phase B — Test Design (before any implementation).** Define "correct" first:
- Port `skein/tests/test_canon_conformance.py` from the branch verbatim — its
  frozen `VECTORS` are the contract: nfc/nfd collapse, `Z`/offset -> UTC,
  fractional-second truncation, whole-second, canonical-bytes carry no raw NUL,
  hash-path and sign-path operate on identical bytes.
- Add Phase-0-specific tests the branch doesn't have: (1) the storage delegation
  produces byte-identical results to `identity.compute_folio_hash` for a `Folio`
  built from each divergent `created_at` encoding; (2) recompute-on-write — a
  `save_folio` of an edited folio changes `content_hash` (the old guard's
  failure, now a passing test); (3) a cross-check harness that hashes N real
  live folios and asserts equality to the reference. These define the contract;
  implementation is "just" making them green.

**Phase C — Implementation (minimal, to green).**
1. Port the canon/identity folio slice (Decision 1) — closed under knurl.
2. Delegate `storage.compute_folio_hash` to `identity.compute_folio_hash`;
   delete the inline body (Decision 2).
3. Move the recompute into `LogDatabase.save_folio`, unconditional; remove the
   once-guard and the `get_folio` read-time mutation (Decision 3).
4. Conformance + new behavior tests green; `fidelity/harness.py check` green;
   the existing suite green.
5. Write `backfill_content_hash.py`; run corpus stats on COPIES; review the
   blast radius before any live write (Decision 4).

**Phase D — Hardening (find what the tests missed).**
- Adversarial pass (Gremlin / oracle): malformed `created_at`, non-str scalars
  under TEXT affinity, empty/None fields, huge content, surrogate/NUL bytes,
  ordering/determinism, a torn WAL run mid-backfill.
- Knuth: line-by-line on the canon port and the migration UPDATE.
- Two-genotype fell (opus + GPT-5.5); a finding clears only when both agree.
- Then apply live per-project with backups + the old->new mapping log, and
  re-run conformance + the fidelity harness against the migrated live data.
