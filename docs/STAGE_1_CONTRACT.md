# Stage 1 — the server-required store contract (from the readonly inventory, spool 71793ff1)

The ONLY `SkeinNextStore` methods a live station SERVER (ingress / web read / envelope /
resolve / redeem) reaches. Everything else on `SkeinNextStore` is authoring/CLI (dropped)
or orphaned. Build the port to THIS contract; match the flagged behaviors exactly.

## Group A — Folios (station reads/writes over the working `versions` table)

Server-live: `create_folio`, `get_folio`, `list_folios`, `search_folios`, `find_by_prefix`,
`folios_in_site`, `folio_site_slug`, `folio_site_slugs`, `latest_statuses`, `count_folios`.
**DEAD (skip): `folios_by_type`, `folios_by_created_by`** (station.py/cli only).

- `create_folio(fields) -> hash` — recompute hash via `identity.compute_folio_hash` (caller
  does NOT pass content_hash), `normalize_created_at`, INSERT OR IGNORE, idempotent.
- `get_folio(hash) -> dict | None` — dict of the 6 folio columns or **None** (never `{}`).
- `list_folios(limit, offset) -> [dict]` — `ORDER BY created_at, content_hash` ASC. Only
  caller passes `limit=1_000_000`, no offset, and re-sorts DESC in Python; the
  hash-tiebreak still leaks through (stable sort) so it must be deterministic.
- `search_folios(query, limit) -> [dict]` — split on whitespace, cap 32 terms; per-term
  `(title LIKE ? ESCAPE '\' OR content LIKE ? ESCAPE '\')` ANDed; `_like_escape` order =
  backslash, then `%`, then `_`; fetch a **candidate window** `ORDER BY created_at DESC,
  content_hash LIMIT max(limit*5, 200)`, then Python `_search_score` (title=3, body=1 per
  term) stable-sorted desc, truncate to limit. Ranking the whole corpus instead breaks
  tiebreak + which rows survive.
- `find_by_prefix(prefix, limit) -> [hash]` — `content_hash LIKE prefix% ESCAPE '\' ORDER BY
  content_hash LIMIT ?`; returns bare hash strings.
- `folios_in_site(site_hash, type=None, limit=None) -> [dict]` — `SELECT DISTINCT f.* ...
  JOIN threads t ON t.from_id=f.content_hash WHERE t.to_id=? AND t.type='within' ORDER BY
  f.created_at, f.content_hash`. Server passes neither type nor limit. **Keep DISTINCT.**
- `folio_site_slug(hash) -> slug | None` — join within→slugs, `ORDER BY s.slug LIMIT 1`
  (alphabetically-first slug on multi-site).
- `folio_site_slugs(hashes=None) -> {hash: slug}` — server calls with None (whole corpus);
  same alphabetical-first rule via Python `setdefault` over `ORDER BY s.slug`.
- `latest_statuses(hashes) -> {hash: status}` — **order-dependent, no SQL aggregate**:
  `SELECT to_id, content FROM threads WHERE type='status' AND to_id IN (...) ORDER BY
  created_at, thread_hash` ASC, then `out[to_id]=content` (last write wins). Absent folios
  omitted; the method NEVER invents `'open'` (callers `.get(h,'open')`). Keyed by folio
  HASH, refs-free — NOT the workbench's refs-slug-keyed `_latest_control_by_folio`.
- `count_folios() -> int` — catalog + `.well-known` totals.

## Group B — Threads (all server-live)

- `save_thread(from_id, to_id, type, weaver, created_at, content) -> hash` — INSERT OR
  IGNORE keyed on `compute_thread_hash(from_id,to_id,type,weaver,created_at,content)`
  (thread_id NOT in the hash), `normalize_created_at`; returns the hash. **Refs-free** — the
  workbench `save_thread` genesis-keys via `_genesis_key_control`/`genesis_of_slug` (refs),
  which a station must NOT do (Risk-3).
- `get_thread(hash) -> dict | None`.
- `get_threads(from_id=None, to_id=None, type=None) -> [dict]` — AND of the non-None
  filters, `ORDER BY created_at, thread_hash` ASC (envelope.py consumes this order for
  dedup + BFS; do not re-sort).

Row-dict contract: threads consumers read exactly `thread_hash, from_id, to_id, type,
weaver, created_at, content`. The working threads table also carries `thread_id` — an extra
key is harmless (targeted key access) but the station write generates a thread_id to satisfy
the column; thread_id is not part of the content address.

## Group C — Slugs / aliases

Server-live: `set_slug`, `resolve_slug`, `list_slugs`, `resolve_alias`.
**DEAD (skip): `register_slug`/`_register_slug_locked`, `set_alias`.**
NOTE: this is the FLAT skein_next slug model. Stage 1 replaces it with the genesis-anchored
`station_slugs` + derived-head resolver (brief-20260708-31bu) — the resolve_slug/list_slugs
CONTRACT (slug→hash, list of (slug,hash)) is preserved, the mechanism changes. `set_slug` is
last-write-wins (ingress uses it, not the guarded register_slug) — preserve that.
`resolve_alias` (legacy-id→hash) stays a flat `aliases` lookup.

## Group D — Federation (DEFERRED to the server stages, tables only in Stage 1)

Server-live set (build WITH ingress/redeem/read later): `add_manifest`,
`add_constituent_attribution`, `get_constituent_proof` (**3-state**: None / dict with
`proof_missing:True` / full dict — do not collapse), `get_binding`, `count_active_operators`
(ingress boot invariant), `get_invite_by_token_hash`, `reserve_redeem_attempt`,
`log_redeem_failure`, `redeem_invite_cas` (calls `add_binding` internally — not dead),
`verify_cache_get`, `verify_cache_put` (ingest path only). Dead: the manifest-proof getters,
operator/binding CLI verbs, invite CLI verbs, `all_manifests`, `backfill_verify_cache`.
Posture note: with `require_signed` OFF (today's default) the write path only touches group D
via `count_active_operators` at boot — confirm the re-homed station's posture before ordering
group-D work.

## 12 subtle behaviors to preserve (differential-test targets)

1. `latest_statuses` — order-dependent overwrite, thread_hash tiebreak, never defaults 'open'.
2. `search_folios` — bounded recency candidate window THEN rank (not whole-corpus rank).
3. `_like_escape` order: backslash, then %, then _ (shared by search + find_by_prefix).
4. `folio_site_slug`/`folio_site_slugs` — alphabetical-first slug, two mechanisms must agree.
5. `get_constituent_proof` 3-state (later stage).
6. None-vs-missing everywhere — never a sentinel dict.
7. `folios_in_site` DISTINCT.
8. Dead-in-practice server-file methods (`all_manifests`, backfill verify_cache) — don't port.
9. `add_binding` fires inside `redeem_invite_cas` — not dead (later stage).
10. Group D only fully exercised under `require_signed` on.
11. offset/limit/type present-but-unexercised — keep the params, don't let them drive design.
12. Row-dict key contract: 6 folio cols / 7 thread cols exactly (extra keys tolerated).
