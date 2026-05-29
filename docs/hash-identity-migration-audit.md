# SKEIN corpus audit: content-hash folio identity migration risk

Read-only audit run 2026-05-29 over every `~/projects/*/.skein/data/skein.db`
(excluding `*_backup*`). 37 databases, 10,217 folios, ~9,274 threads total.

Method: python + sqlite3, all connections opened `mode=ro`. Scripts live in
`.cache/hash_audit*.py`; raw per-project numbers in `.cache/hash_audit_result.json`.

## Headline

Content-hash folio identity is collision-free across the existing corpus. No two
folios share either the (created_by, created_at) axis or the full canonical tuple
(type, created_at, created_by, title, content). The real migration work is not
collisions — it is thread endpoints, and only a small slice of those are folio
references at all.

---

## Q1 — Timestamp precision and (created_by, created_at) collisions

10,214 of 10,217 folios carry sub-second (microsecond ISO) precision.
3 folios are whole-second resolution — all in speakbot, all hand-seeded with
round-number times:

playbook-nvyc by=unknown at=2025-12-21T10:30:00Z
brief-20251114-horizon by=cc-session-20251114 at=2025-11-14T20:00:00Z
issue-20251114-mill by=cc-session-20251114 at=2025-11-14T20:01:00+00:00

(created_by, created_at) non-unique rows: ZERO in every project, corpus-wide.
Even the three second-resolution rows have distinct (actor, timestamp) pairs.

Risk: none today. The only latent weakness is those 3 seeded second-res rows —
a collision there would require a future folio created by the same actor in the
same wall-clock second. Negligible, but worth normalizing those 3 rows to
microsecond precision (or salting the hash) before relying on timestamp in the key.

Outlier: speakbot is the only project with any second-resolution timestamps.

## Q2 — Identical canonical-tuple collisions

Folios sharing an identical (type, created_at, created_by, title, content) tuple:
ZERO collision groups, ZERO collision rows, in every one of the 37 projects.
No examples to show — there are none. Each existing folio maps to a distinct hash.

Note on Q1 vs Q2: (created_by, created_at) non-uniqueness is necessary but not
sufficient for a content-hash collision (the hash also covers type/title/content).
Both came back zero, so the question is moot either way, but Q2 is the binding
constraint and it is clean.

## Q3 — Threads by type, and the non-folio endpoint split

Thread type distribution (corpus totals):

status 4928
message 2154
tag 1251
reference 650
mention 175
reply 73
succession 40
assignment 3

Of the from_id / to_id endpoints that are NOT a folio_id in the same db, there are
5,035 occurrences. Refined classification:

actor/agent name 2519 (50.0%)        e.g. physis-0306, burr-0215, metis-1228
short hex / shard id 1626 (32.3%)    e.g. 820509c2, 24bbfc6f; plus 9 shard-MMDD
other-textual 737 (14.6%)            e.g. cc-session-20251107, cc-new-session, prime, cleanup, next-agent
folio-id-shaped (dangling) 147 (2.9%)  e.g. brief-20251226-axg8, brief-20260511-unaw
cross-project (colon) 6 (0.1%)       e.g. warp:brief-20260526-yqhg (all in portfolio)

The actor-authored vs cross-project split at scale: roughly 96-97% of non-folio
thread endpoints are actor / agent / session identities. This is by design —
threads connect folios to the agents who authored or touched them, not only
folio-to-folio. Only ~153 endpoints (147 bare folio-shaped + 6 colon cross-project)
are folio references that do not resolve in their own db.

Per-type semantics (which side is the folio), which matters for any rewrite:

message — from_id is the sender/actor (2077 of 2154 non-folio), to_id is the folio
  subject (2057 of 2154 folio). The single largest actor bucket.
reference — almost purely folio to folio (646 of 650 from-folio, 645 to-folio).
  This is the clean folio-link type; near-zero dangling.
tag — both endpoints mostly actor-namespaced (agent-MMDD on both sides).
status — overwhelmingly folio to folio (4810 from-folio, 4849 to-folio); the
  non-folio cases are actors/sessions setting status (qm-0318, cc-session-*).
mention — from_id always a folio; to_id mostly folio, ~30 are labels (turn-1, ai-sdk).
succession — connects SESSIONS, not folios (see Q5).

Migration implication: a content-hash remap only needs to rewrite folio endpoints.
Actor/session endpoints are stable strings and stay as-is. The dangling 147 + 6
cross-project refs already don't resolve under today's id-based scheme; the
migration is the moment to decide a lookup/remap policy for them, but it does not
make them worse.

Outliers:
- speakbot dominates: 6,322 of ~9,274 threads (68%).
- folkprotocol is unusually shard-id heavy (104 short-hex endpoints, mostly from tags) for its size.
- portfolio is the only project with explicit colon cross-project refs (6, all pointing into warp).
- skein itself is reference- and mention-heavy (373 reference, 152 mention) — the dogfooding project.

## Q4 — Multi-site membership

site_id is a single column, and the data respects that: ZERO folios are effectively
in more than one site, in every project. No folio_id appears with two distinct
site_ids anywhere. Nothing surprising — membership is strictly single-valued.

## Q5 — Supersedes / succession threads

Only one thread type carries succession semantics: `succession`, 40 total
(speakbot 38, skein 2). No supersedes/replaces/follows types exist.

Critical finding: succession threads connect agent SESSIONS, not folios. Of the 80
endpoints across 40 succession threads, only 2 are folios — the rest are session
identities (cc-session-20251109, cc-session-20251111, cc-session-20251107, the-tester,
cc-skein-maintainer). So folio-to-folio supersession effectively does not exist in
the corpus as thread data.

Chain shape: the session-succession graph is not cleanly linear. speakbot has 5
branch nodes (one from_id pointing to multiple successors) and 3 merge nodes
(multiple predecessors into one). Example branch:

cc-session-20251111 -> cc-session-20251107, cc-session-20251109, the-tester, cc-session-20251111

skein's 2 succession threads form no branches.

Migration implication: there is essentially no folio-supersession chain to
re-point under a new identity scheme. The branching that exists is in session
lineage, which is keyed on session/actor names, not folio ids — untouched by a
folio-hash change.

---

## Summary of migration risk

1. Hash collisions: none in the current corpus, on either axis. Safe.
2. Timestamp precision: 3 seeded second-resolution rows in speakbot; normalize or
   salt before depending on timestamp in the key. Otherwise microsecond throughout.
3. Thread rewrite scope: ~half of thread endpoints are actor identities (stable,
   untouched). The clean folio-to-folio type is `reference` (650, near-fully
   resolvable). status/message are folio-heavy on one side.
4. Dangling / cross-project folio refs: 153 endpoints (147 bare folio-shaped + 6
   colon) already don't resolve in-db. Already broken under id identity; needs an
   explicit remap/lookup decision at migration time, but hash migration neither
   helps nor harms them.
5. Multi-site: not a concern; strictly single-valued.
6. Supersession chains: absent for folios; the succession graph is about sessions,
   keyed on actor/session names, so no folio-chain re-pointing burden.

Single biggest caveat: speakbot is 72% of all folios and 68% of all threads, and is
the sole source of every precision and branching outlier. Validate the migration
against speakbot specifically — it is not representative in volume, but it is where
every edge case lives.
