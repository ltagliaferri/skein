# Phase 1+2 design note — address by hash, edit becomes commit

Status: DESIGN-GATE artifact, revised after a two-genotype fell. The fell found
blocking holes (fixed below) and confirmed one phasing decision: **state
(status / assignment / archive) is NOT re-anchored to the genesis hash in Phase
2 — that slips to Phase 3.** No code is changed by this note. Grounded in the
live tree as it stands at `7bcf05b` (Phase 0 merged), not in the briefs'
summaries.

Parent: brief-20260626-fmfs (one content-addressed model; "Resolved data model").
Phase 1 brief: brief-20260626-1d7a (address-by-hash + slug-as-head ref).
Phase 2 brief: brief-20260626-ut0n (edit-as-commit + LAYER-WIDE head filtering).
Grammar: docs/ADDRESSING_GRAMMAR.md. Prior phase: docs/PHASE_0_RESEARCH.md.

Phase 1 and Phase 2 **ship merged as one unit**. Shipping hash addressing while
edits still mutate in place lets an external consumer (shuttle resolves
`sha256::`) cache an address that dangles the moment the folio is edited. Both
genotypes in the 1d7a fell asked for the merge; this note treats the two briefs
as one deliverable and sequences them as one commit chain.

**What the deferral changes, up front.** Phase 2 now does content/identity
immutability and head-filtered reads. It does **not** retire the `folios` table,
does **not** drop `folios.status`, and does **not** convert any state thread from
slug-keyed to hash-keyed. State keeps running on its existing mechanism
end-to-end through Phase 2 (slug-keyed `status`/`assignment` threads + the
`folios.status`/`assigned_to`/`archived` columns + the existing
`enrich_folios_with_status` / `get_current_status` / `get_current_assignment` /
`get_latest_statuses` path). The whole contract — dropping `folios`, dropping the
status column, and re-anchoring state threads to the genesis hash — is Phase 3.

---

## 1. The end state in one paragraph

Today one `folios` row holds everything about a folio: the five hashed identity
fields (`type, title, content, created_at, created_by`) mixed with mutable local
control (`status, assigned_to, archived, site_id, …`). Editing does an
`INSERT OR REPLACE` on that row (storage.py:1053), so an edited folio's prior
content is destroyed and its old hash dangles. Phase 2 introduces two new tables
beside `folios`: an immutable **versions** table holding every version ever
written keyed by content hash, and a mutable local **refs** table mapping each
slug to its lineage's head version while caching a copy of the control columns.
The existing **threads** table gains the `supersedes` edge (new→old) and a
`reverted` marker. After Phase 2, every folio **content/identity** read is a query
over **heads** (`refs.head_hash = versions.content_hash`) joined to `refs`;
by-hash reads go straight to `versions` and see superseded content too. The
`folios` table stays alive as a dual-written state remnant (its control columns
still back the unchanged state path), and is dropped — along with the status
column and the slug→genesis state move — in **Phase 3**, not here.

---

## 2. The three-table schema, mapped onto the live store

### 2.1 What exists now (ground truth)

- `folios` (storage.py `_init_db`, 438): `folio_id` TEXT PK, `type`, `site_id`,
  `created_at`, `created_by`, `title`, `content`, `status` DEFAULT `'open'`,
  `assigned_to`, `target_agent`, `omlet`, `archived` INTEGER DEFAULT 0,
  `metadata` JSON, `acknowledged_at`, `content_hash`. Indices on `site_id`,
  `type`, `status`, `assigned_to`, `created_by`, `created_at DESC`. The table and
  all six indices are created **unconditionally** (`CREATE TABLE IF NOT EXISTS`)
  on every `LogDatabase` construction.
- `folios_fts` (503): FTS5 external-content table, `content=folios`,
  `content_rowid=rowid`, indexing `folio_id, title, content`. Kept in sync by
  triggers `folios_ai` / `folios_ad` / `folios_au` (AFTER INSERT/DELETE/UPDATE,
  515-534) — also created unconditionally.
- `threads`: `thread_id` TEXT PK, `from_id`, `to_id`, `type`, `content`,
  `weaver`, `created_at`. Indices on `from_id`, `to_id`, `type`, `created_at`,
  and the composite `(from_id,type,created_at DESC)` / `(to_id,type,created_at DESC)`.
- `migrate_folios_from_json` (1250) runs on **every** `JSONStore.__init__`
  (1448) and its idempotency guard queries `folios` (`SELECT COUNT(*) FROM
  folios`, 1260). It assumes `folios` exists. It `INSERT`s `folios` rows
  **directly** (1321-1346, `INSERT OR IGNORE`), bypassing `save_folio` — so it is a
  third `folios`-write path that must also populate `versions`/`refs` in Phase 2
  (§5/§6); otherwise a fresh legacy-JSON import after the read-flip would have
  `folios` rows with no head and read empty.

After Phase 0, `folios.content_hash` is the correct `sha256::<hex>` of the
current content, recomputed unconditionally at the single write chokepoint
`LogDatabase.save_folio` (storage.py:1037-1038). That value is the seed for the
migration below: today's `content_hash` is exactly the head version's hash.

### 2.2 What is added

**`versions`** — immutable, append-only, the content-addressed object store:

```
CREATE TABLE versions (
    content_hash TEXT PRIMARY KEY,   -- sha256::<hex>, the identity
    type         TEXT NOT NULL,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   DATETIME NOT NULL,
    created_by   TEXT NOT NULL
)
```

Only the five hashed fields. No slug, no status, no site — those are not part of
identity. A row is written once and never updated or deleted. `content_hash` is
computed by `identity.compute_folio_hash` (the Phase 0 RSP), so a version's PK is
verifiable from its own columns. Two lineages that arrive at byte-identical
content share one row (content-addressing dedups); that is correct, not a
collision, and the migration must use `INSERT OR IGNORE` to tolerate it.

`created_by` lives **here**, on `versions` — it is one of the five hashed
identity fields. It is **not** on `refs` (see §2.2 refs and the §4 filter fix).

**`versions_fts`** — FTS5 external-content over `versions`, indexing
`content_hash, title, content`, with an **AFTER INSERT trigger only**. Versions
are append-only — never updated, never deleted — so neither an `_au` nor an
`_ad` trigger is needed. (The earlier draft proposed an AFTER DELETE trigger;
it is dead code because no version row is ever deleted, so it is dropped.) This
replaces `folios_fts` as the search index when reads switch (commit C). It
indexes *all* versions including superseded ones; search restricts to heads via
the `refs` join (§4).

**`refs`** — mutable, local, never federated; the naming + control layer:

```
CREATE TABLE refs (
    slug         TEXT PRIMARY KEY,    -- the folio_id (brief-YYYYMMDD-xxxx)
    genesis_hash TEXT NOT NULL,       -- the lineage id = first version's hash
    head_hash    TEXT NOT NULL,       -- current head version (a versions.content_hash)
    site_id      TEXT NOT NULL,
    status       TEXT DEFAULT 'open', -- copy-of-column cache of folios.status
    assigned_to  TEXT,                -- copy-of-column cache of folios.assigned_to
    archived     INTEGER DEFAULT 0,   -- copy-of-column cache of folios.archived
    target_agent TEXT,
    omlet        TEXT,
    acknowledged_at DATETIME,
    metadata     JSON
)
```

Indices mirroring the filterable surfaces: `site_id`, `status`, `assigned_to`,
`archived`, `head_hash`, `genesis_hash`. The slug stays the PK and the everyday
human handle. `genesis_hash` is the stable lineage identity (survives edits);
`head_hash` moves on every identity-changing edit.

**The control columns on `refs` are, in Phase 2, a copy-of-column cache, not a
thread rebuild.** "Control columns" here means the **full non-identity set**:
`status, assigned_to, archived, site_id, target_agent, omlet, acknowledged_at,
metadata` — every `Folio` field that is not one of the five hashed identity
fields. After commit C the join reconstructs the whole `Folio` from
`refs⋈versions` and stops reading `folios`, so the refs copy must carry all of
them or the omitted ones vanish from API output; every copy site below (create
branch §3.1, the §3.2 refresh, the §5 repair/backfill, `migrate_folios_from_json`)
copies this full set. Because state is not re-anchored, `refs.status` /
`refs.assigned_to` / `refs.archived` are written by copying the same values that
flow into the `folios` columns (the `Folio` object's fields at the write
chokepoint, §3), in the same transaction. They are *not* derived from threads in
Phase 2. The source of truth for state remains exactly what it is today: the
slug-keyed `status`/`assignment` threads plus the `folios` columns, read through
the unchanged `enrich_folios_with_status` path (§4). Genesis-anchored,
thread-derived control is Phase 3 work.

`refs` carries every field of the old `folios` row that is **not** one of the
five hashed fields — except `created_by`, which is hashed identity and lives on
`versions`. `type` is also intentionally absent: type is a hashed identity field
(it lives only in `versions`), and the addressing grammar's edit rule forbids
changing type on edit ("a different type is a different lineage",
ADDRESSING_GRAMMAR.md §"Edit rule"), so a slug's type is fixed for its whole
lineage and is read through the head version.

### 2.3 What is altered

- The `threads` table gains two new `type` values in Phase 2: `supersedes` (the
  edit edge, `from_id` = new hash, `to_id` = old hash) and `reverted` (the
  revert marker, §3.4, `from_id` = prior head hash, `to_id` = the reused hash).
  `ThreadType` in models.py:125 is a closed `Literal` (`message, mention,
  reference, assignment, succession, reply, tag, status`); adding these is an
  **API-contract change**, not a pure addition — the parent brief flags this
  ("Thread vocabulary is NOT purely additive"). `succession`/`status`/
  `assignment` stay. No blanket rename. The supersedes and reverted edges'
  endpoints are content **hashes**, not slugs — the first hash-keyed edges in
  the table, the first thread endpoints that are neither a `folio_id`/slug nor
  an `agent_id`. **Every endpoint-resolution surface keyed on "the endpoint must
  be a known folio_id or agent_id" must exclude the `supersedes` and `reverted`
  types, or it will report every edit edge as broken.** Concretely, the thread
  orphan-detector `find_orphaned_threads` (client/analytics.py:9, surfaced by the
  harness probe `stats-threads-orphan`, harness.py:224) builds `valid_ids` from
  `folio_id`s and flags any `from_id`/`to_id` not in that set; its hash endpoints
  are legitimately absent, so Phase 2 must filter `type IN ('supersedes',
  'reverted')` out of orphan detection (and any analogous endpoint-resolution UI)
  before the membership test. This is a Phase 2 change shipped in the same commit
  that adds the edges (commit A introduces the types; the orphan-detector exclusion
  ships no later than commit B, when the first edges are actually written).
  **`archive` is NOT added in Phase 2**: archive stays a plain
  `folios.archived` column mutation (routes.py:925-926) with no thread, exactly
  as today; the archive self-loop belongs to the deferred Phase 3 state move.
- `save_folio` (storage.py:1031) grows the edit-as-commit logic (§3). The Phase 0
  recompute at 1037-1038 stays and becomes the hash that decides
  identity-change vs no-op. It **also** keeps doing today's `INSERT OR REPLACE`
  into `folios` (dual-write through Phase 2).
- Every `folios` content/identity read in storage.py — including the three
  module-level raw-SQL readers (§4) — is repointed at `versions`⋈`refs` heads.
  `_row_to_folio` (storage.py:1229) is paralleled by a `_row_to_folio_from_join`
  that reconstructs the same `Folio` shape from the joined row, so route output
  is byte-identical.

### 2.4 What is NOT dropped in Phase 2 (deferred to Phase 3)

Nothing from the old shape is dropped in Phase 2. Stated explicitly so the
deferral is unambiguous:

- **`folios.status` is NOT dropped.** It remains the column fallback in the
  unchanged state path and the source the `refs.status` cache copies from.
- **The `folios` table, `folios_fts`, and their three triggers are NOT
  dropped.** `folios` stays the dual-written state remnant. Its content columns
  become vestigial (no longer read after commit C) but the row and its control
  columns stay live.

When Phase 3 *does* drop `folios`, that commit MUST, **in the same commit**:

1. Remove the `folios` + `folios_fts` + `folios_ai`/`folios_ad`/`folios_au` DDL
   from `_init_db` (storage.py:438/503/515-534) — they are `CREATE … IF NOT
   EXISTS` and will otherwise re-create the table on the next construction,
   making the drop futile and 500-ing every request that still expects the new
   shape.
2. Gate or remove `migrate_folios_from_json` (storage.py:1250), which runs on
   every `JSONStore.__init__` (1448) and queries `folios` at 1260 — it would
   throw on a missing table.

Both points are Phase 3 constraints, recorded here so the contract commit cannot
forget them. They do not apply within Phase 2 because Phase 2 never drops
`folios`.

---

## 3. The edit-as-commit write path

All external writes funnel through `LogDatabase.save_folio` (storage.py:1031) —
the same single chokepoint Phase 0 chose, reached from routes.py:705, 928, 1954.
The route layer already splits an edit into: title/content mutated on the folio
object (routes.py:889-893); status → a slug-keyed `status` thread
`from_id=to_id=folio_id` (896-906); assignment → a slug-keyed `assignment`
thread (911-921); archived mutated on the object (925-926); then one `save_folio`
(928). Folio creation (routes.py:705) and the hypothesis verdict path
(routes.py:1941-1980, which writes a slug-keyed `status` thread then
`save_folio`) follow the same shape.

**Phase 2 leaves every one of those route-level state-thread writes exactly as
they are.** Because state is not re-anchored, `save_folio` does **not** mint or
move any `status`/`assignment`/`archive` thread — doing so would double-write
state. The only new write responsibility `save_folio` takes on is maintaining
`versions`/`refs` and the `supersedes`/`reverted` edges. It additionally copies
the folio object's `status`/`assigned_to`/`archived`/`site_id` into the `refs`
cache columns (a column copy, not a thread), in the same transaction.

### 3.1 One atomic version/ref maintenance method

Today `save_folio` is a single `INSERT OR REPLACE` and cannot tell a create (no
prior row) from an edit (existing row) — `INSERT OR REPLACE` collapses both.
Phase 2 adds an explicit **create-vs-edit branch** plus one transactional
storage method that mints a version, writes the supersedes edge, and moves
`refs.head_hash` **atomically** (single connection, single transaction, with the
`folios` dual-write):

- **Create branch** — `SELECT head_hash FROM refs WHERE slug = ?` returns no
  row. This is a new lineage: `INSERT INTO versions` the genesis row, `INSERT
  INTO refs` (`slug`, `genesis_hash = head_hash = new_hash`, control columns
  copied from the folio object). No supersedes edge (nothing to supersede).
  **The control copy is the FULL non-identity set from §2.2 —
  `site_id, status, assigned_to, archived, target_agent, omlet,
  acknowledged_at, metadata` — not just `{status, assigned_to, archived,
  site_id}`.** After commit C the join reconstructs the whole `Folio` from
  `refs⋈versions` and never reads `folios`, so any non-identity column left out
  of the refs copy (`target_agent` on a brief, `omlet`, `metadata`) silently
  vanishes from API output. The create route sets all of these
  (routes.py:699-702), so the loss would be broad and immediate. The §8
  conformance test gains an assertion that a brief's `target_agent` and a
  folio's `metadata` survive a content edit when read back through the join.
- **Edit branch** — a `refs` row exists. Compare the recomputed
  `new_hash` against `refs.head_hash` and dispatch §3.2 / §3.3 / §3.4.

`created_at` and `created_by` are **not** recomputed on edit — they inherit the
lineage genesis (§3.5), so a pure status edit cannot change the hash through a
timestamp, and the `== head_hash` test is exact.

**Ordering hazard — genesis fields must be reasserted BEFORE the hash recompute,
not after.** Phase 0's recompute (storage.py:1037-1038) runs at the very top of
`save_folio`, *before* the create-vs-edit branch, and it hashes the live folio
object's `created_at`/`created_by` as-handed. If a caller hands an edit with a
fresh `created_at` (or a different `created_by`), the recompute folds that fresh
value into `new_hash`, while §3.2 then stores the genesis `created_at`/`created_by`
in the `versions` row — so the version's PK would be computed from one timestamp
and stored against another, and `compute_folio_hash` would **not** verify the row
against its own columns. This must not depend on caller discipline. So on the edit
branch, before the recompute: read the lineage's genesis `created_at`/`created_by`
(`SELECT created_at, created_by FROM versions WHERE content_hash =
refs.genesis_hash`) and overwrite them onto the folio object. Only then recompute
`new_hash`. Concretely, the edit branch must resolve `refs` (and load the genesis
fields) *ahead of* the line-1037 recompute — the recompute moves below the branch
resolution for edits, or the genesis reassert is hoisted above it. The create
branch keeps the caller's `created_at`/`created_by` as genuine genesis values and
recomputes against them unchanged.

### 3.2 Minting a new version (identity field changed)

When `new_hash` is not already in `versions` and `new_hash != head_hash` (title
or content changed; type is frozen by the edit rule):

1. `INSERT INTO versions` the new row: `new_hash` + the five fields, with
   `created_at`/`created_by` taken from the genesis (§3.5).
2. `INSERT INTO threads` a `supersedes` edge: `from_id = new_hash`,
   `to_id = old_head_hash`, `type = 'supersedes'`, `weaver = <editor agent>`,
   `created_at = now()`. The **edge** carries the per-edit editor and time — the
   version carries neither. **Threading the editor:** because `created_by` is now
   frozen to the genesis author, `save_folio` cannot infer the per-edit editor
   from the `Folio` object. The editor must be passed in explicitly — add an
   optional `editor` (a.k.a. `weaver`) argument to `JSONStore.save_folio` /
   `LogDatabase.save_folio`, supplied by the edit route from its `x_agent_id`
   (routes.py:857-886), with a defined fallback (`'unknown'` / the genesis author)
   for direct storage callers and tests. Do not try to recover the editor from
   `folio.created_by` — that is the genesis author by design.
3. `UPDATE refs SET head_hash = new_hash` for the slug. The slug stays; the
   control cache is refreshed from the folio object — the **full non-identity set
   from §2.2** (`status, assigned_to, archived, site_id, target_agent, omlet,
   acknowledged_at, metadata`), so the refreshed cache stays complete; only the
   head pointer moves.

The old version row is untouched and stays addressable by its hash forever — the
Phase 1+2 durability guarantee.

### 3.3 Status / assignment / archive in Phase 2 (unchanged path)

State is written by the **routes**, exactly as today, and `save_folio` does not
touch it:

- status: route writes a slug-keyed `status` thread (`from_id = to_id =
  folio_id`, routes.py:896-906) and sets `folio.status`.
- assignment: route writes a slug-keyed `assignment` thread (`from_id =
  folio_id`, `to_id = <assignee>`, 911-921) and sets `folio.assigned_to`.
- archived: route sets `folio.archived` (925-926); no thread.

`save_folio` copies `folio.status` / `folio.assigned_to` / `folio.archived` into
the `refs` cache columns in the same transaction as the `folios` write, so the
cache never lags the column. Reads source these from `refs` (§4), and
`enrich_folios_with_status` still overrides status/assignment from the slug-keyed
thread replay — unchanged. The genesis-anchored self-loop design (one uniform
hash-keyed state edge, federation-ready) is **deferred to Phase 3** and is not
built here.

**Create-path ordering fix (the `refs` cache must not be seeded with defaults).**
The edit route sets `folio.status`/`folio.assigned_to` on the object *before* its
`save_folio` (routes.py:889-928), so the `refs` cache copy is correct on edit. The
**create** route does the opposite: it calls `save_folio` first with the hardcoded
placeholders `status="open"` / `assigned_to=None` (routes.py:689-705), and only
*afterward* writes the sugar `status`/`assignment` threads from
`metadata["status"]` / `assigned_to` (routes.py:724-753). With the copy-of-column
cache, that order seeds `refs.status='open'` / `refs.assigned_to=NULL` for a folio
that was created *with* a non-open status or an assignee — the cache (and the
`refs.status`-derived `by_status` stat, §4) drifts from the sugar state from birth.
`enrich_folios_with_status` still corrects single-folio GET and list output by
replaying the sugar threads, so the user-visible read is right — but the `refs`
cache and the stats that read it directly are wrong. Fix at the create route: set
`folio.status` / `folio.assigned_to` from the sugar inputs (`metadata["status"]`,
`folio_create.assigned_to`) **before** the create `save_folio`, so the genesis
`refs` cache is written with the true initial state in one transaction. (The
re-sync-after-sugar alternative — a second `refs` UPDATE after the thread writes —
is rejected: it adds a write and a window; set-before-save is the chosen option.)
The sugar threads are still written afterward as today; only the object's fields
move ahead of the save.

### 3.4 The revert / DAG re-entry no-op

If an edit's `new_hash` already exists in `versions` (the agent edited content
back to a prior value), do **not** mint and do **not** write a new supersedes
edge — a second supersedes edge into an existing node would form a cycle in the
DAG. Instead:

1. `UPDATE refs SET head_hash = new_hash` — move the head pointer back to the
   existing version, and refresh the control cache from the folio object.
2. Write a durable history marker: a thread `type = 'reverted'`, `from_id =
   prior_head_hash`, `to_id = new_hash` (the reused version). This is a real,
   queryable thread — **not** a note on the derived activity feed — so "reverted
   to X" survives and is auditable, while introducing no second `supersedes`
   edge and therefore no cycle. The original `supersedes` chain stays walkable.

The discriminator is purely `SELECT 1 FROM versions WHERE content_hash =
new_hash` — cheap, exact, and idempotent. Note the dedup corollary
(content-addressing): one `versions` row can be the head of two different refs
when two lineages reach byte-identical content. That is coherent; the head
predicate is per-ref (§4), so it handles a shared head without ambiguity. The
seeded conformance test (§8) covers both the revert marker and the
two-refs-share-one-head case.

### 3.5 created_at / created_by inheritance

A new version inherits `created_at` and `created_by` from the lineage **genesis**
(the first version). Author is author: a folio another agent edits keeps its
original `created_by`. Per-edit editor and time live on the supersedes **edge**
(`weaver`, `created_at`), not on the version (fixed decision).

Consequence for **activity semantics**: because `created_at` inherits genesis, an
edit does **not** bump a folio up the activity feed — which is exactly today's
behaviour (the activity route sorts by the folio's stored `created_at`, and
editing already does not change it). So inheritance *preserves* activity
ordering; choosing now()/editor would have shifted it.

---

## 4. The head predicate and every read surface

**Head predicate (global, authoritative):** a version is the head of a slug iff
`refs.head_hash = versions.content_hash`. Full stop. A content/identity read
lists exactly one row per ref by joining `refs` to `versions` on that equality.

**The "no incoming `supersedes` edge" form is NOT a valid global head
predicate** and must not be used as one. Revert (§3.4) and dedup (§3.3 corollary)
both let a version carry an incoming `supersedes` edge yet still be a current ref
head — e.g. after `v1→v2→v3` then a revert to `v2`, `v2` has the incoming edge
`v3 supersedes v2` but is the head. The edge form is demoted to **lineage
verification only**, scoped to a specific ref/genesis: a `verify`/`rebuild` path
walks the `supersedes` edges *within one lineage* (from `refs.genesis_hash`) to
confirm the chain is acyclic and reaches `refs.head_hash`. It is never used to
decide headship across the table.

Every content/identity read that today selects from `folios` must, after the
read-switch commit (C), select **heads of `versions` joined to `refs`** and apply
each filter against the correct table:

- **identity filters → `versions`**: `type`, `created_by`.
- **control filters → `refs`**: `site_id`, `status`, `assigned_to`, `archived`.

(`created_by` is a hashed identity field on `versions`, not `refs` — the earlier
draft filtered it on `refs`, which is wrong.)

The exact surfaces in storage.py and what each needs:

- `get_folio(folio_id)` (1085) — **get-by-slug**. Resolve slug →
  `refs.head_hash` → the `versions` row; join `refs` for control. Returns the
  head, as today.
- `get_folios(...)` (1096) — list. `SELECT … FROM refs JOIN versions ON
  refs.head_hash = versions.content_hash` + filters. Naturally one row per
  lineage (one ref per lineage), i.e. heads-only.
- `search_folios(query)` (1156) — FTS. Match `versions_fts`, then **join through
  to heads before ranking**: `JOIN versions ON versions.content_hash =
  versions_fts.content_hash JOIN refs ON refs.head_hash = versions.content_hash
  WHERE versions_fts MATCH ? ORDER BY rank LIMIT ?`. The head join must precede
  `ORDER BY rank LIMIT`, or superseded matches fill the top-N and hide head
  matches (today's query is `JOIN folios_fts … ORDER BY rank LIMIT`,
  storage.py:1163-1169). BM25 ranking drifts once edits exist because the index
  carries every version — accepted (risk 2).
- `get_folio_count(site_id)` (1173) — count of **lineages** = `COUNT(*) FROM
  refs` (optionally `WHERE site_id = ?`). Not `COUNT` of versions.
- `get_folio_stats(site_id)` (1184) — `by_type` from `versions` at the head
  (join), `by_status` from `refs.status` (the copy-of-column cache), total from
  `refs`.
- `get_site_last_activity()` (1214) — `MAX(created_at)` per site; `created_at` is
  the genesis timestamp (inherited), `site_id` is on `refs`, so this groups `refs
  JOIN versions` (on `head_hash`) by `refs.site_id`. Values unchanged vs today.
- `move_folio(folio_id, dest)` (1134) — a **second `folios`-write path outside
  `save_folio`** (it `UPDATE folios SET site_id` directly, storage.py:1144-1147).
  Site is ref-local and not an identity field, so a move mints no version. But
  because it bypasses `save_folio`, it must carry the dual-write itself: from
  commit B it becomes a **dual-write transaction** — `UPDATE refs SET site_id = ?`
  **and** `UPDATE folios SET site_id = ?` in one transaction — and mints no
  version. Its return reconstructs the head `Folio` from the `refs`⋈`versions`
  join along with the rest of the read-flip at commit C (in commit B it still
  returns `_row_to_folio` off the `folios` row). The `folios` half of the dual-write
  persists through Phase 2 and is removed only when `folios` is dropped in Phase 3.
  See the §5 invariant, which names `move_folio` explicitly.

**Three module-level raw-SQL readers also select from `folios` directly and
bypass the `LogDatabase` methods.** They open their own read-only sqlite
connections and must be flipped in the same commit (C), or a durable by-hash
citation and the cross-project surfaces silently read the wrong shape:

- `search_folio_across_projects(folio_id, …)` (storage.py:101) — existence check
  `SELECT 1 FROM folios WHERE folio_id = ?`. Flip to existence per-slug on
  `refs`: `SELECT 1 FROM refs WHERE slug = ?`. Called from routes.py:881 (and the
  update path at 165/193).
- `get_project_last_activity_timestamps()` (storage.py:153) — `SELECT
  MAX(created_at) FROM folios`. Flip to `MAX(versions.created_at)` over heads:
  `SELECT MAX(v.created_at) FROM refs r JOIN versions v ON v.content_hash =
  r.head_hash`. Called from routes.py:1885 (`/projects/timestamps`).
- `resolve_folio_across_projects(folio_id, …)` (storage.py:189) — `SELECT * FROM
  folios WHERE folio_id = ?`, then builds a `Folio`. Flip to reconstruct the
  `Folio` from `refs`⋈`versions` head (identity from the `versions` head row,
  control from `refs`). Called from routes.py:973 (and 165/193).

Route surfaces over the `LogDatabase` methods inherit the fix for free:
`get_activity` (routes.py:1112 → `get_folios`), `unified_search` folios branch
(routes.py:1228 → `search_folios`/`get_folios` + `enrich_folios_with_status`),
`get_site_folios` (routes.py:499 → `get_folios`).

**`enrich_folios_with_status` (utils.py:97) is UNCHANGED in Phase 2.** It keeps
replaying the slug-keyed `status`/`assignment` threads via `get_latest_statuses`
(storage.py:965, keyed on `threads.to_id = slug`) and `get_latest_assignments`
(997, keyed on `from_id = slug`), with the column value as fallback. The fallback
column now arrives via `refs.status` (the copy-of-column cache reconstructed into
the `Folio`) instead of `folios.status`, but the keying, the override, and the
output are identical. No genesis keying anywhere in Phase 2.

**Determinism under the swap.** `get_folios` today sorts `ORDER BY created_at
DESC` (storage.py:1129). Under the `versions`⋈`refs` join, `versions.created_at`
ties can reorder rows versus the old `folios` rowid order, drifting the harness
with no behavioral intent. Add a deterministic secondary key: **`ORDER BY
created_at DESC, slug`** (slug = `refs.slug`, the PK, unique and stable). Apply
the same secondary key anywhere a `created_at` sort decides output order.

**The head filter lands in the same commit that repoints reads at `versions`**
(commit C, §6). It cannot land earlier (nothing reads versions yet) or later
(the first read of versions without it returns superseded rows and the read
surfaces go red across find/search/stats/activity at once — the LAYER-WIDE scope
the ut0n fell named).

---

## 5. The versions/refs backfill (mirror Phase 0)

Phase 2 ships **one** migration. The genesis-anchored state-thread migration is
**deferred to Phase 3** (§9).

`skein/migrations/backfill_versions_refs.py`, modeled exactly on
`backfill_content_hash.py`: registry-driven, `--dry-run` (read-only, safe on a
live WAL db), `--backup` (online-backup API), idempotent, per-db, the whole
read-compute-write inside one `BEGIN IMMEDIATE` transaction with a busy timeout,
FTS-trigger set asserted unchanged before/after, one bad db isolated and
reported, blast-radius stats printed before any write. Proven on copies of real
data first; busy projects (skein, speakbot) quiesced for the window.

It seeds `versions` and `refs` from the existing `folios` rows — pure expansion,
no existing data altered: each folio → one `versions` row (`INSERT OR IGNORE`,
`content_hash` + the five fields) and one `refs` row (`slug = folio_id`,
`genesis_hash = head_hash = content_hash`, control columns **copied** from the
folio's `status`/`assigned_to`/`archived`/`site_id`/…). This runs at commit A and
its rollback is "drop the new tables".

**Commit A/B window invariant.** Two things can happen to `folios` between the
commit-A backfill and the commit-B dual-write going live, and the catch-up must
handle **both**:

1. A folio **created** in the window exists in `folios` but is absent from
   `versions`/`refs` — it would vanish at the commit-C read-flip.
2. A folio **edited** in the window (its `folios` content/`content_hash` mutated
   in place by the still-old `save_folio`, or its `site_id` moved by `move_folio`)
   already has a stale `refs.head_hash` pointing at the pre-edit version and a
   *new* current content that was never minted into `versions`. "Seed any slug not
   yet in `refs`" skips this row (it's already in `refs`) and leaves the head stale
   and the current version un-minted — so the read-flip would surface old content.

So the catch-up is a **REPAIR pass, not an insert-missing pass**. **Commit B
begins with an idempotent repair under `BEGIN IMMEDIATE`**, then enables dual-write
in the same deploy. For **every** `folios` row (not only those missing from
`refs`): `INSERT OR IGNORE INTO versions` the row's current-content version
(`folios.content_hash` + the five fields), `INSERT OR IGNORE INTO refs` if the slug
is new (genesis = head = `content_hash`, control copied), and for an existing ref
`UPDATE refs SET head_hash = folios.content_hash` **and re-copy the control columns**
(`status`/`assigned_to`/`archived`/`site_id`/…) from the `folios` row. This makes
`refs` a faithful mirror of `folios`-current regardless of what happened in the
window. Merging commits A and B into one is the alternative that closes the window
structurally (no live gap to repair); the repair catch-up keeps the commits smaller
and is the recommended path.

From the instant dual-write is live, the invariant holds: **no `folios` write
occurs without a matching `versions`/`refs` write in the same transaction.** Note
this binds *every* `folios` writer, not just `save_folio`: `move_folio`
(storage.py:1134) is a second `folios`-write path and is made a dual-write
transaction in commit B (it `UPDATE`s both `refs.site_id` and `folios.site_id`
together, §4); `migrate_folios_from_json` (storage.py:1250) is a third and is given
versions/refs population in Phase 2 (§6). After commit B no path mutates `folios`
without the companion `versions`/`refs` write.

---

## 6. Sequenced commit order (additive + green, no red intermediate state)

The invariant held at **every** commit: no read surface ever returns a non-head,
and `fidelity/harness.py check` is green. The expand/contract shape keeps the old
`folios` content read path live until the new one is built and verified — but the
**contract half (drop) is Phase 3**, so Phase 2 is expand + dual-write + read-flip
only.

- **Commit A — expand schema (additive).** `CREATE TABLE versions`, `refs`,
  `versions_fts` + its AFTER INSERT trigger in `_init_db`. Add `supersedes` and
  `reverted` to `ThreadType`. Run `backfill_versions_refs.py` on the live db
  (after copy-proof). Nothing reads the new tables; `folios` is still the
  read+write source. Harness green (no read path changed).

- **Commit B — dual-write (additive).** Begin with the idempotent **repair**
  catch-up under lock (§5) — repair, not insert-missing, so a folio edited or moved
  in the A→B window has its head re-pointed and version minted, not just newly-seen
  slugs seeded. Then make **every** `folios` writer carry the companion
  `versions`/`refs` write:
  - `save_folio` maintains `versions`/`refs` and the `supersedes`/`reverted` edges
    with full edit-as-commit semantics (§3) — including the genesis-fields-before-
    recompute ordering fix (§3.1) — refreshes the `refs` control cache from the
    folio object, **while still** `INSERT OR REPLACE`-ing the `folios` row as today.
  - `move_folio` (storage.py:1134) becomes a dual-write transaction: `UPDATE` both
    `refs.site_id` and `folios.site_id` together, mint no version (§4).
  - `migrate_folios_from_json` (storage.py:1250), which `INSERT`s `folios` rows
    directly on every `JSONStore.__init__` (1448) and bypasses `save_folio`, also
    populates `versions`/`refs` for each row it inserts (or invokes the §5 seed when
    it inserts anything) — otherwise a fresh legacy-JSON import after Phase 2 would
    get `folios` rows with no `versions`/`refs` and read empty post-C (§5).
  - The create route sets `folio.status`/`folio.assigned_to` from the sugar inputs
    **before** its `save_folio` so the genesis `refs` cache is not seeded with
    placeholder defaults (§3.3).
  - The thread orphan-detector / endpoint-resolution surfaces exclude the
    `supersedes`/`reverted` hash-keyed edge types (§2.3) — the first edges of those
    types are written here.

  Route-level state-thread writes are untouched. Reads still hit `folios`, so they
  stay green. A verification script gates the next commit (rebuild `refs` from
  `versions` + walk the `supersedes` chains, diff against `folios` row-for-row —
  §8). Immutable writes are happening, but to tables nothing reads yet, so no filter
  is needed and nothing is red.

- **Commit C — flip reads + head filter (the load-bearing commit).** Repoint
  every storage.py content/identity read (§4), **including the three
  module-level raw-SQL readers**, at `versions`⋈`refs` heads-only, and search at
  `versions_fts` with the head join before ranking. The head predicate
  (`refs.head_hash = versions.content_hash`) lands **here**, in the same commit
  that first exposes `versions` to reads — the atomicity the ut0n fell requires.
  `folios` stops being read (its content columns go vestigial) but is **not**
  dropped; its control columns still feed the unchanged state path via the
  `refs` cache and `enrich_folios_with_status`. Heads == the prior `folios` set
  because A backfilled and B maintained them, so output is identical. **This commit
  REDS the fidelity harness unless the frozen fixture db is backfilled in the same
  commit** — the harness boots against a frozen `skein.db` that has `folios` but no
  `versions`/`refs`, so the moment reads flip to the join they hit empty tables and
  find/search/stats/activity go red at once (§8). The fix is part of commit C, not a
  follow-up: run the `versions`/`refs` backfill inside `build_fixture()` (or ship a
  pre-migrated fixture db). Because the fixture is edit-free, `heads == folios`, so
  the joined reads produce **byte-identical** output and the **existing blessed
  baseline stays valid with NO re-bless**. Do **not** "fix" a red harness here by
  re-blessing against empty output — that silently destroys the gate. Gate: the
  **seeded multi-version conformance test** (§8) plus harness green on the
  backfilled fixture.

- **Commit D — build and wire the Phase 1 resolver (additive).** The rev3 `::`
  grammar exists in `address.py` but is **unwired dead code**: routes import the
  legacy parser (`from .address_legacy import parse`, routes.py:44), and
  `address.resolve` / `StationIndex` (address.py:610/224) are an unimplemented
  `Protocol` with no concrete backing. This commit **builds** a concrete
  `StationIndex` (over `versions`/`refs`) and the `address.py` ref edits (§7),
  then **wires** routes to dispatch slug/hash/ref forms through `address.parse` +
  `resolve` while the legacy parser stays the default for `project:id`. `versions`
  now makes a by-hash address durable (it resolves a superseded version too),
  which is why Phase 1 ships with Phase 2 and not before.

Phase 2 ends at commit D. **No contract commit ships in Phase 2.**

Which changes MUST be atomic: the read-flip and the head filter (within commit
C) — they are the same edit. Which can land independently: schema expansion (A),
dual-write (B), and the resolver (D) are each their own green commit. B is
deliberately split from C so the new tables can be verified live before any read
depends on them; the fell may prefer to merge B+C into one atomic commit (§8) —
both orderings preserve the no-leak invariant.

The contract (drop `folios`/`folios_fts`/triggers + remove their DDL from
`_init_db` + gate `migrate_folios_from_json`; drop `folios.status` after a
slug-anchored status-only column backfill; re-anchor state to the genesis hash) is
**Phase 3**, sequenced in §9.

---

## 7. Phase 1: build and wire the resolver and the refs naming layer

The rev3 `::` grammar already lives in `skein/address.py`; the ref layer does
not, and the grammar is not yet reachable from the API (routes use
`address_legacy`). Per ADDRESSING_GRAMMAR.md §"The ref layer", refs require these
exact `address.py` edits (verified against the live module):

- A **1-segment production** for a bare `<slug>` — `parse()` (address.py:501) has
  no single-token form today; a bare slug falls through to the 2-/3-segment
  branches and raises.
- A **`ref` dispatch before the 2-segment bare-hash branch** (address.py:524) —
  `ref::<slug>` would otherwise hit the `<algo>::<digest>` rule and die as
  "unsupported hash algorithm: ref".
- A **target kind on the address** — `ParsedAddress` (address.py:167) types every
  address's folio as a `Hash`; a ref needs a slug-carrying variant (`Target =
  Hash | Ref` or a `ref` type + `slug` field), and `construct()` (569) needs a
  ref case to round-trip.
- **Ref-aware builders** — `_build_alias` (539) / `_build_web` (545) hardcode
  segment 2 as the hash algorithm; each needs a `segment == "ref"` branch that
  builds a ref target instead of calling `_make_folio`.
- **Add `ref` to `_RESERVED_ALIASES`** (address.py:51) — so no station may be
  nicknamed `ref`.
- A **ref branch in `resolve()`** (610) **and a slug→head method on
  `StationIndex`** (224). `resolve()` today reads `parsed.folio.digest` and
  lengthens a short hash via `folios_with_prefix` (624); a ref has a slug, not a
  digest, so it needs its own path that asks the station for the head of
  `<slug>`. The `StationIndex` `Protocol` gains `head_of_slug(slug) ->
  Optional[str]`.

`StationIndex` is today a bare `Protocol` (address.py:224) with no
implementation. Commit D **builds a concrete `StationIndex`** backed by the live
store:

- `folios_with_prefix(algo, prefix)` → `SELECT content_hash FROM versions WHERE
  content_hash LIKE ?` (scans **all** versions, including superseded, so a short
  hash lengthens against the full history — a durable by-hash citation must
  resolve forever).
- `head_of_slug(slug)` → `SELECT head_hash FROM refs WHERE slug = ?`.

Then **wire routes**: the call sites currently do `parse_address(folio_id)` from
`address_legacy` (routes.py:44, used at 866 and elsewhere). The wiring dispatches
on form — a single-colon `project:id` still parses through `address_legacy`,
while slug / `ref::<slug>` / `sha256::` / `hash::sha256::` forms parse through
`address.parse` and resolve through the concrete `StationIndex`. `address_legacy`
stays live and is the default for `project:id`; its full retirement (and the
migration of its `client/cli.py` / `routes.py` callers) is out of scope here.

The slug validator is the existing `_ALIAS_RE` (byte-identical grammar:
`[a-z0-9-]`, alphanumeric ends, ≤32). Resolution acceptance after this lands:

- `sha256::<64-hex>` and `hash::sha256::<64-hex>` — full hash → the exact version.
- short prefix (8–63 hex, station-scoped) → lengthen via `versions` prefix scan;
  ambiguous prefix raises (existing `AmbiguousShortHash`).
- `<slug>` / `ref::<slug>` — local head via `refs`.
- `project:id` (single-colon) — **unchanged**, through `address_legacy`.

**Same-station slug collision: refuse, do not guess.** If `refs` ever returns
more than one head for a slug (possible only under the current mint-and-hope
weakness), `ref::<slug>` raises, the same posture as an ambiguous short hash. Slug
minting hardening is a separate later task and is **not** a blocker for this
phase; ref resolution must only refuse on the rare collision, never pick a head.

Definition of done for the Phase 1 slice: round-trip resolution for `sha256::`,
short prefix, `ref::<slug>`/bare slug, and `project:id`, each alongside the
others; the single-colon scheme unregressed; the harness green.

---

## 8. Verification — the harness is blind to the load-bearing change

**State plainly: `fidelity/harness.py check` CANNOT catch a head-filter bug, and
does not exercise several flipped surfaces.** Its fixture is **frozen legacy data
with no edits** (harness.py fixture/PROBES), so every lineage has exactly one
version — `versions == heads`. A read that *omits* the head filter returns
identical output on that fixture, because there is no superseded row to leak. The
harness also blanks `content_hash` (so `head_hash` exposure is invisible to it)
and has **no probe** for single-folio GET, the hypothesis verdict flow,
cross-project cascade (`resolve_folio_across_projects`), or `/projects/timestamps`
(`get_project_last_activity_timestamps`). The harness is necessary but **not
sufficient** as the gate for commit C.

**The real gate for commit C is a SEEDED multi-version conformance test.** It is
the only thing that proves head-filtering. It must, on a fresh db:

1. Mint `v1` for a slug, then edit it to `v2` (distinct content → new head).
2. Assert `get_folios` / `get_folio` / `get_folio_count` / `search_folios` /
   `get_folio_stats` / `get_site_last_activity` (and the route-level activity and
   unified-search surfaces) return **only `v2`** — never `v1`.
3. Assert a **by-hash fetch of `v1`** returns `v1`'s exact bytes (the five
   immutable fields), with the historical-content flags below.
4. Cover **revert** (§3.4): edit `v2` back to `v1`'s content, assert the head
   returns to `v1`, that a `reverted` thread exists, and that no cycle formed.
5. Cover **dedup** (§3.3 corollary): two slugs reach byte-identical content,
   share one `versions` row, and each ref resolves its own head independently.
6. Cover the three module-level raw readers and the single-folio/hypothesis/
   cascade/timestamps surfaces the harness skips.
7. Cover **`move_folio`** (§4): move a slug to a new site and assert it
   dual-wrote — `refs.site_id` **and** `folios.site_id` both updated in one
   transaction — minted no new version (head hash unchanged), and returned the
   joined head `Folio` carrying the new site. Also assert a create-with-sugar
   (non-open status / an assignee at creation) lands the true state in the genesis
   `refs` cache and the `by_status` stat, not the `'open'`/`NULL` placeholders
   (§3.3).

**The commit-C fixture-backfill requirement (no re-bless).** Repointing reads at
`versions`⋈`refs` reds the harness unless the frozen fixture `skein.db` carries
`versions`/`refs` rows. Commit C must therefore run the `versions`/`refs` backfill
inside `build_fixture()` (or ship a pre-migrated fixture) **as part of the same
commit that flips reads**. Because the fixture is edit-free (`heads == folios`),
the joined reads emit byte-identical output and the existing blessed baseline
remains valid — **do not re-bless**. A harness that goes red here is the missing
backfill, not a changed contract; re-blessing against the empty-join output would
silently gut the gate.

**By-hash control semantics.** A by-hash fetch of a *superseded* version returns
its **five immutable fields plus an explicit flag set** — `is_head = false` and
`lineage_head = <current head hash>` — so a consumer can tell the content is
historical and which version is lineage-current. A bare hash is **not** a ref, so
the fetch does **not** attach arbitrary current control (status/assigned_to): a
hash addresses content, not a lineage's mutable state. A by-hash fetch of a
*head* version returns `is_head = true`. This distinction is asserted by the
seeded test (step 3) and is part of the API contract.

**An explicit B→C verification script** gates the read-flip, instead of leaning
on "harness green": rebuild `refs` from `versions` (walk the `supersedes` chains
from each `genesis_hash` to a single head) and diff the rebuilt heads + control
cache against the live `folios` rows, row-for-row. The diff must include
`site_id` so a `move_folio` that updated only one of the two tables is caught
(the move is the one dual-write that touches a control column outside
`save_folio`). Any divergence blocks commit C.

**Migration / rollback safety.** `backfill_versions_refs.py` mirrors Phase 0:
`BEGIN IMMEDIATE` so a concurrent writer waits rather than tears; online-backup
snapshot before any write; idempotent re-runs; old→new mapping logged as rollback
evidence; FTS trigger set asserted identical before/after; copy-proof before
live; busy projects quiesced. Because the Phase 2 migration is pure expansion (no
existing row altered), its rollback is "drop the new tables". The risky,
thread-rewriting migration is the Phase 3 re-anchor (§9), not anything in Phase 2.

**Open risks the fell should scrutinize:**

1. **FTS over all versions vs heads-only.** `versions_fts` indexes every version,
   so a superseded edit's body stays in the index and every search post-filters to
   heads. This is correct but wasteful, and the trigger fires once per version
   mint. BM25 ranking drifts once edits exist (the index carries non-head bodies).
   Index-all-versions + post-filter is **accepted**; the cheaper alternative
   (index only head content, re-index on head moves) trades append-only
   simplicity for update churn. Either is green on the frozen harness data.

2. **Revert / dedup edge cases.** §3.4's no-op relies on the `versions` membership
   test. The fell should probe: two lineages reverting into a shared version (one
   `versions` row as the head of two refs); a revert that re-enters a version
   mid-chain (history must stay walkable without a cycle); and the `reverted`
   marker shape. The seeded test (steps 4–5) is the executable check.

3. **By-hash control display.** A by-hash fetch returns immutable content with
   `is_head`/`lineage_head` and no mutable control. The fell should confirm this
   is the intended surface and that consumers (shuttle) read the flags.

4. **Commit B/C split vs merge.** Splitting dual-write (B) from the read-flip (C)
   adds a transient where `versions`/`refs` are maintained but unread; merging
   them matches the ut0n "same commit" letter but is a larger review. Both
   preserve the no-leak invariant; the fell picks the tradeoff.

### What is decided vs what still needs a call

Decided (fixed inputs, baked in): Phase 1+2 ship merged; **state stays
slug/column-based through Phase 2 — the genesis re-anchor is deferred to Phase
3**; **`folios` is not dropped in Phase 2 — the whole contract is Phase 3**;
`refs` control columns are a copy-of-column cache, not a thread rebuild;
`created_by` lives on `versions` (identity), and identity filters hit `versions`
while control filters hit `refs`; the head predicate is `refs.head_hash =
versions.content_hash` (the edge form is lineage-verification only);
`created_at`/`created_by` inherit genesis, editor/time on the edge; revert moves
the ref, writes a durable `reverted` thread, and mints nothing; supersession only
on an identity-field change; status-only/assignment/archive do **not** mint
versions; a by-hash fetch returns the five fields + `is_head`/`lineage_head` and
no mutable control; the slug collision refuses; `address_legacy` stays the
`project:id` default; the seeded multi-version test (not the harness) is the real
gate for commit C, backed by the B→C rebuild-and-diff script; every commit
additive + green; migrations copy-proofed with backups. **Write-path completeness
(baked in):** every `folios` writer carries the companion `versions`/`refs` write —
`move_folio` is a dual-write transaction from commit B (both `site_id` columns, no
version), and `migrate_folios_from_json` populates `versions`/`refs` in Phase 2;
the commit-A→B catch-up is a **repair** pass (re-point heads + mint current-content
versions for edited/moved rows), not insert-missing; the create route sets
`status`/`assigned_to` on the folio **before** the genesis `save_folio` so the
`refs` cache and `by_status` stat are seeded with true state, not placeholders; the
edit branch reasserts genesis `created_at`/`created_by` **before** the line-1037
hash recompute so a version PK always verifies against its own columns; the
`supersedes`/`reverted` hash-keyed edges are excluded from orphan-detection and
endpoint resolution; commit C backfills the frozen harness fixture in the same
commit and the edit-free fixture stays byte-identical with **no re-bless**.

Still needs a call (surfaced, not silently resolved): risk 1 (FTS index breadth /
BM25 drift); risk 4 (merge commits B and C); whether commit B's window is closed
by the catch-up backfill or by merging A+B (this note recommends the catch-up).
The state re-anchor's migration risk is **no longer an open call for this fell** —
it is deferred to Phase 3, where it will be scoped and proven on its own.

---

## 9. Deferred to Phase 3 (named here so Phase 2 is honest about its boundary)

Phase 2 deliberately stops short of the contract. Phase 3 owns:

1. **The slug→genesis state re-anchor.** Convert `status`/`assignment` threads
   from slug-keyed to genesis-hash-keyed (threads become content-addressed edges),
   add the `archive` self-loop, and rebuild `refs` control columns from the
   re-anchored threads (cache becomes thread-derived, not copy-of-column). This is
   the riskiest migration (it rewrites the busiest thread data) and gets its own
   RSP-style proof. The old §5-step-2 re-anchor corruption concern lives here now,
   not in Phase 2.

2. **The column-only status backfill, scoped narrowly.** If `folios.status` must
   be captured before the column drops, mint **slug-anchored** `status` self-loops
   (`from_id = to_id = slug`) for any folio whose non-open status is not already
   carried by a `status` thread. This is **not** re-anchoring — it is filling the
   column's content into the existing slug-keyed thread space so the column can
   drop. Scope it strictly to `type IN ('status')` self-loops: never touch an
   `assignment` thread's `to_id` (the assignee) and never touch `reference`
   threads.

3. **The drop.** Drop `folios.status`, then `folios` / `folios_fts` / the three
   triggers — and, **in the same commit**, remove that DDL from `_init_db`
   (storage.py:438/503/515-534) and gate/remove `migrate_folios_from_json`
   (storage.py:1250/1448, which queries `folios` at 1260). Stop the `folios`
   dual-write in `save_folio`. Reads have been on the new shape since commit C, so
   this is a pure contraction.
