# Phase 4 design gate — a thin, curlable `publish` on the working server-based skein

Status: DESIGN GATE (RSP phase 1 output). Written first-hand with Patrick
(gremlin-0703, 2026-07-04). Settled-decisions folio: `finding-20260704-alma`.
Supersedes the premise of `brief-20260704-92xo` and refines `finding-20260704-hp0d`.
Fell this gate (two-genotype, opus + codex) before any code.

> Note to a fresh agent: §2 is not optional colour. The single most expensive
> mistake here is building Phase 4 on the wrong codebase, and the brief that
> launched this points you at exactly that wrong codebase. Read §2 before you
> touch anything.

---

## 1. What Phase 4 is, and its boundary

Phase 4 gives the **working** skein a `publish` capability: turn selected local
folios (and the edges among them) into a **signed RFC-6962 Merkle manifest batch**
and push it to a remote station, so that content federates with a verifiable
provenance chain.

Three commitments fix the shape (each argued in later sections):

1. **`publish` is a local API route on the working skein server (port 8001), not a
   CLI-only verb** (§4). It is curlable and automatable; the CLI `publish` is a thin
   wrapper over the same route. This is the load-bearing paradigm call: a capability
   a tool cannot reach by HTTP is a capability that broke API-first.
2. **What travels is author-declared, not inferred from thread type** (§5). The
   manifest is the explicit folio+thread set the author names; the system signs what
   was declared, adding and dropping nothing. Type-based classification is demoted
   from a gate to an advisory linter.
3. **Reuse the abandoned rewrite's federation machinery, rebuild only the authoring
   half thin** (§8). The remote ingress server and the crypto/manifest libraries are
   sound and are reused as-is; only the "read what to publish" path is rebuilt to sit
   behind the working server's API.

**Out of scope (stated so the boundary is honest):**
- Instance↔instance peer federation (`/fed/v0`). It does not exist today (only named
  aspirationally in `skein_next/ingress.py:8`; `resolve.py:85` defers remote `web::`
  resolution). Phase 4 is client→instance publish only. Peer federation is a later
  mesh phase and gets its own gate.
- Re-thinning `skein_next`'s local **authoring** verbs (post/set_status/roster). That
  is the abandoned-rewrite cleanup, a separate track (`brief-20260621-aeh9`, now on
  hold — see §2).
- The read-side "whose status wins" question when the same folio hash carries
  competing signed statuses from different signers (§5.4). A display/render concern,
  deferred.
- Operator-side ingress policy (banning dangling, moderation, allowlists). Future,
  and it belongs to the receiving operator, not the author (§5.3).

**Discipline (RSP, non-negotiable):** two-genotype fell on the gate, the test
design, and the implementation; additive to the running system (each step keeps it
behaving identically until publish is deliberately turned on); **do NOT change
`compute_thread_hash` or the `canon.py` hashing** (§7); fail closed on a missing
precondition.

---

## 2. The system model — read this first (it is not intuitive)

There are **two** codebases both called some flavour of "skein," and telling them
apart is the whole game.

### 2.1 The working skein — server-based, thin client, CANONICAL

- A real API server: `skein_server.py` + `skein/routes.py`, running as
  `skein.service` on `http://localhost:8001`. You can curl it and do everything.
- A **thin** client: `client/cli.py` (the `skein` command, per
  `pyproject.toml: skein = client.cli:main`). Its real work is `requests.request(...)`
  against 8001 — folios, threads, activity, search, briefs, sites, roster all go over
  the API ("Make HTTP request to SKEIN API", `client/cli.py:155`; threads fetched as
  "1 API call vs N*2", `:1140`).
- The storage layer `skein/storage.py` is the **server's**. This is where the Phase 3b
  `thread_hash`-PK swap landed. **3b did not leak into the client** — verified: a grep
  of all of `client/` for `skein.storage` / `threads_pk_swap` / `compute_thread_hash`
  / `save_thread` / `get_threads_display` / `JSONStore` / `LogDatabase` returns
  nothing. The only direct `skein` imports in the client are the shard engine (an
  intentional local-git carve-out, zero-HTTP by design, older than 3b), the backup
  ops verb, and two pure utilities (name generation, address parsing). The migration
  and cutover ran as one-shot scripts with `skein.service` quiesced. **The working
  system is still clean.**

### 2.2 skein_next / interskein — the ABANDONED fat-client rewrite, REFERENCE ONLY

- `pyproject.toml: interskein = skein_next.cli:main`. It worked, but it broke the
  paradigm: its local **authoring** verbs open the local store **in-process** as a
  library (`skein_next/cli.py:56` `_open_station` → `Station(data_dir)`, then
  `station.post(...)` straight against `.skein-next/store.db`). No server hop. That is
  the "fat client" pattern, and it is **why the interskein cutover was pulled**
  (Patrick). Keep it as reference; do not make it canonical, do not build the author
  side on it.

### 2.3 Why the federation machinery is still usable despite the abandonment

The fat-client sin is confined to **local authoring**. The federation surfaces never
had it, because they *are* servers or pure functions:

- **Ingress** (`skein_next/ingress.py`, port 9101) is a real FastAPI write server:
  `POST /publish/v0/folios`, `POST /invite/redeem`. It receives a batch, verifies it,
  stores it. Server-based, tested (`test_e2e_publish.py`, `test_require_signed.py`,
  `test_verify_manifest.py`, `test_manifest_store.py`).
- **Web read** (`skein_next/web/app.py`, port 9001) opens the store `read_only=True`
  (`web/app.py:212`) and serves GETs only. Server-based.
- **Crypto/manifest libraries** (`canon.py` Merkle, `sign.py`, `signing.py`,
  `wire.py`, `envelope.py`, `profile.py`) are pure functions. Note: `canon.py` and
  `signing.py` are **byte-identical forks** of the `skein/` copies (the Merkle block
  `canon.py:171-359` diffs clean; `signing.py` is line-for-line). The working skein
  therefore **already has** the Merkle primitives and `signing.py` — so the crypto is
  not even a port, it is already present on the server side.

### 2.4 The one-line model

> The working skein (8001) is your private workbench. A station is a public instance
> = web-read (9001) + ingress-write (9101), two processes over one store. They meet at
> exactly one moment: **publish**, when your workbench signs a selection and POSTs it
> to a station's ingress. Phase 4 builds that one arrow, on the workbench side, as a
> curlable route.

Publish is a client→**remote**-server interaction by nature, so it carries **no
fat-client sin**: the only local work is selection and signing (authoring intent and
identity), and the trust logic (verify, membership, store, attribute) lives on the
remote server. See §4 for why we still expose it as a *local* route.

---

## 3. Runtime topology

Three servers, two tiers.

- **Tier 1 — private authoring (local):** the working skein API on **8001**. Your
  source of truth; the thin CLI curls it. (The 8000 range is reserved for legacy; the
  station deliberately uses 9xxx.)
- **Tier 2 — a public station:** web-read on **9001** (`read_only`) + ingress-write on
  **9101** (`read_write`), run together as two processes over one store, one mount
  `:ro` one `:rw`, nginx-fronted on the live box. The read surface physically cannot
  mutate the store; only the ingress can, and only through the verify path.

Publish is the single arrow from a Tier-1 workbench to a **remote** Tier-2 ingress.
Published content then appears on that station's 9001 read surface. The two tiers do
not share a store or a process; they share only the signed batch on the wire.

---

## 4. The publish path — a local, curlable API route

### 4.1 Why a local API route and not a CLI-only verb

A CLI-only `publish` cannot be reached by a tool over HTTP — automation would have to
shell out to the binary or reimplement assembly+signing+POST. That breaks API-first
for the one operation, in the one place it most matters. So publish is a **route on
the working skein server** (a new handler in `skein/routes.py`), and the CLI
`publish` verb is a thin wrapper that curls it — identical to how every other verb
already works.

The old aversion ("agents hate talking to the local CLI to do something remote") was
an aversion to the *CLI-shell* shape, not to a local API mediating a remote call. The
latter is normal and good (`git push`, `docker push`): a local server holds your
identity and reaches out on your behalf. The route gives the good version.

Because publish is client→remote by nature, routing it through the *local* server
adds no fat-client sin (the server already owns the store it reads from, and the
remote server does the verification). It buys curlability, which is the point.

### 4.2 The request/response contract

`POST http://localhost:8001/publish` (name TBD-in-impl; the shape is fixed here):

Request body:
- `to`: the target station's publish URL (e.g. `https://ingress.<station>`), which the
  route canonicalizes and POSTs to at `/publish/v0/folios`.
- **the declared set**, one of:
  - `select`: `{refs?: [...], site?: "..."}` — a selection the server resolves into a
    *proposed* set (the reachability proposal, §5.2). Used with `dry_run` to preview.
  - `manifest`: `{folios: [<hash>...], threads: [<thread_hash>...]}` — an **explicit**
    author-declared set. The server signs exactly this (§5.1).
- `token`: an OIDC token from a prior 1-click login (§6). Absent ⇒ the route refuses
  to sign (fail closed); an unsigned posture, if kept at all, is a separate explicit
  flag, never the default.
- `dry_run`: when true, the server assembles + lints + returns the set and warnings
  **without signing or sending** (signing burns an irreversible Rekor entry, so it is
  gated to the real send — mirrors `skein_next/publish.py:276-286`).

Response:
- On `dry_run`: `{proposed: {folios, threads}, warnings: [...]}` — the editable
  proposal plus lint warnings.
- On a real send: the instance ack, per-constituent (`accepted` / `existing` /
  `rejected{reason}`, folios and threads separately), plus the recorded local
  publish-state and the `warnings` that did not block.

### 4.3 What the route does server-side (all inside the 8001 server, reading its own store)

1. Resolve the declared set — either the explicit `manifest`, or `select` → proposal.
2. Run the linter over it (§5.3); collect warnings; **do not block** on any of them.
   The linter is **total** — it never raises (a malformed row is the physics veto's
   job), so it is safe to run ahead of physics.
3. Enforce the **physics floor** (§5.5, fail closed): each folio/thread's bytes
   reproduce its content hash; the manifest is internally consistent.
4. Build the batch (`wire`-shape, §7) and the Merkle manifest over
   `folio content_hash ++ thread_hash` (kind-agnostic, `canon.merkle_root_for_addresses`).
5. Sign the `{root, leaf_count}` descriptor with `token` under
   `skein.manifest.canon/v1` (§6).
6. POST to the remote ingress; on ack, record local publish-state and return the ack
   + warnings.

The propose→edit→confirm loop is **stateless across calls**: the caller does a
`dry_run` to get the proposal, edits it, and sends the finalized `manifest`. No
server-side session, no interactive server state — API-clean.

---

## 5. Author-declared admission — the model

### 5.1 The declarative core

The manifest **is** the explicit folio+thread list the author declares. The system
signs what was declared: it adds nothing and drops nothing. This is the whole
admission model. There is no predicate deciding what is "allowed."

Rationale (the point Patrick made, recorded because it is easy to backslide into):
inferring intent from a thread's `type` is fragile. `status` means two different
things (private workflow vs. a deliberate published assertion); every new type needs
a classifier edit; and the classifier is a security-relevant gate that can silently
admit or drop the wrong row. Replacing it with "the author says what travels" removes
a whole class of bug and answers the "closed on the public station" want for free
(§5.4).

### 5.2 The proposer (convenience, not authority)

Naming every thread hash by hand is hostile, so the server offers a **proposal**: the
reachability set — edges among the selected folios. This is `skein_next`'s current
`_closed_threads` computation (`publish.py:148`) **demoted from a silent filter to an
editable suggestion**. It rides in the `dry_run` response; the author edits it; the
finalized set is what the real call declares. The proposer is `dry_run` (collect +
show) plus an edit step — most of it already exists.

### 5.3 The linter (advisory — warns, never blocks)

Over the declared/proposed set, server-side, emit warnings. None block a publish:

- **dangling** — a declared edge whose target folio is not in the set. **Allowed**
  (Patrick: "there might be reasons to dangle"), warn. It resolves lazily if that
  folio ever lands elsewhere.
- **slug endpoint** — an edge endpoint that is not a content hash (a local slug /
  agent id). It cannot resolve on another machine, so warn "won't resolve
  off-station." Still publishable.
- **looks-local** — a `status`/`tag` self-loop, or other control/commentary. Warn
  "usually local; keep it?" Still publishable if declared. The legacy
  `classify_row` / `manifest_eligible` knowledge (`skein/migrations/threads_pk_swap.py`)
  is the ready-made source for this heuristic — **demoted from gate to warning text.**

The receiver relaxes to match: `skein_next/ingress.py`'s closed-graph
`present()` "dangling endpoint" **hard reject** (`ingress.py:311-312`) becomes
**accept-and-flag**. Safe: an edge's `thread_hash` is a signed Merkle leaf whether or
not its endpoints are held locally; `manifest_membership` never required the endpoints
be present. Operator-side bans on dangling are a future receiving-operator policy
(§1 out-of-scope).

### 5.4 "Closed on the public station" is legitimate

If you want a folio to read `closed` on a station, **declare its status thread in the
manifest**. No classifier needed. Open nuance, deferred to a display phase: the same
folio is the same content hash on every station, and `status_of` is latest-writer-
wins; two signers publishing the same hash with different statuses collide unless
`status_of` is made signer-scoped. That is a read/render decision, **not** an
admission one, and out of scope here.

### 5.5 Physics vs advisory — the robust line

Exactly two constraints are **not** intent; they are enforced and fail closed:
- **Integrity** — each folio/thread's canonical bytes reproduce its content hash, and
  the manifest root recomputes from its leaf list. (The `wire.*_reject_reason` floor
  runs even in the current unsigned posture.)
- **Resolvability is *not* enforced** — a slug endpoint is publishable; it just draws
  a warning. It is a fact about the world (a slug means nothing elsewhere), surfaced,
  not a gate.

Everything else — type, control, commentary, dangling — is advisory. The author
declares the set; physics is the only veto.

The 3b re-anchor pays off exactly here: structural edges in the working store now
carry **content-hash endpoints**, so a declared structural edge drops straight into a
manifest as a resolvable leaf. Slug-keyed rows (orphans, Class C) simply draw the
resolvability warning.

---

## 6. Signing — reuse the 1-click login, unchanged

Signing is the existing Sigstore flow; there is **no credential-lifecycle problem to
solve** (an earlier draft invented one — deleted).

- The interactive step is a **1-click Sigstore login** on the author's machine that
  yields an OIDC **token** (`skein_next`'s `login` command already prints one). This
  is client-side by nature and stays a separate step.
- The publish route takes that **token** and builds the signer from it — the signer
  accepts a token directly, not only an interactive flow (the `--oidc-token` path,
  `skein_next/cli.py` `_build_signer`). It signs
  `profiled_preimage(skein.manifest.canon/v1, manifest_descriptor_canonical_bytes(root, leaf_count))`
  via `signing.sign`, exactly as `skein_next/sign.py:sign_manifest` does today.
- Location does not change signing: whether publish lives in a client verb or the
  local route, the token is what signs and the login is the same 1-click. In the route
  shape the token simply travels one hop (caller → local route). No held credential,
  no refresh, no lifecycle.
- Security posture stated on purpose: the route signs only with a real token from an
  actual login — it is **not** "anything on localhost can publish as you." A caller
  must have logged in. Whether to add a further gate on the publish route specifically
  is an open call (§10), not a default.

`MAX_LEAVES` is checked **before** the Sigstore ceremony (in `skein/publish.py`,
counting distinct raw leaf data via `canon.dedup_leaf_count` — the same basis the
verifier caps on) so an over-cap publish fails without wasting a Rekor entry.

### 6.1 What a signature binds — destination-agnostic, signer-not-author (decided)

Two trust facts a consumer must not mistake, decided with Patrick and confirmed by
the two-genotype fell (both genotypes flagged the first independently):

- **Destination-agnostic (intended).** The signed descriptor is `{root, leaf_count}`
  only — it names no target station, origin, or audience. So a signed batch (bodies +
  `manifest_signature`, all public; the signature is also on Rekor) can be re-hosted to
  any station that trusts the signer, which will verify and store it, attributed to the
  signer. This is intended: content is meant to spread, re-hosting is idempotent, and a
  signer **cannot restrict which stations carry content they have signed** — like a
  signed commit you can push anywhere. (Contrast the redeem ceremony, which deliberately
  binds `origin`/`route`/`nonce`; the manifest deliberately does not.) A signature is
  authenticity, never confidentiality or scope — access control is the station's,
  separate.
- **The signer vouches; `created_by` is a claim.** `created_by` is one of the five
  hashed folio fields — self-asserted at authoring time and **not** authenticated by the
  signature. The signature proves only that a Sigstore identity **vouched for** these
  content hashes. So a signer can mint a folio stamped `created_by: alice` and sign it
  under their own identity; the receiver records the real signer separately
  (`constituent_attribution.subject`), so the crypto is intact, but the two are different
  actors. Decided: **the signed identity is the only authenticated actor**; `created_by`
  is an unverified claim; a read/render path must NEVER present `created_by` as verified
  authorship. (`created_by` kept as the literal field; "vouched" is the signer's
  relation — language may iterate.) The signed-provenance **verdict already does this
  right**: `envelope.folio_verdict` presents the verified signer subject
  (`SIGNED — {subject} (verified)`), never `created_by`. The remaining gap is a
  byline: the reused read template (`skein_next/web/templates/folio.html`) still labels
  `created_by` as plain "author"; that must be relabeled as a *claim* in the display
  phase (§5.4, open #3) — the one read-surface exception to §8's "reuse as-is."

---

## 7. What travels, and what must not change

The wire batch is `skein_next`'s existing shape (`wire.py`), reused verbatim:

```
{ "protocol": "skein-publish/v0",
  "folios":  [{content_hash, type, title, content, created_at, created_by}, ...],
  "threads": [{thread_hash, from_id, to_id, type, weaver, created_at, content}, ...],
  "site_slugs": { <site content_hash>: <slug>, ... },
  "manifest_signature": {
     "descriptor": {root, leaf_count},          # the SIGNED body
     "leaf_list":  [sorted deduped addresses],   # rides UNSIGNED as transport
     "signature_bundle": <Sigstore bundle json>,
     "issuer": ..., "subject": ... } }
```

The signed bytes are the descriptor only; `leaf_list` travels unsigned so the receiver
can recompute the root. `folio content_hash ++ thread_hash` is the leaf set,
kind-agnostic.

**Hard constraints (fail the fell if violated):**
- **`compute_thread_hash` and the `canon.py` hashing are untouched.** The Merkle
  functions are additive and already present on both sides; publish must not alter the
  thread-identity hash or the folio canon.
- **Additive to the running system.** No existing route/behaviour changes; `publish` is
  new; the ingress `present()` relaxation (§5.3) is the only change to the receiver and
  it only *widens* what is accepted.
- **Fork discipline.** The working server uses `skein/`'s own `canon.py`/`signing.py`
  (already present). Do **not** import the working server from `skein_next`. If a
  manifest builder (`sign.py`) or wire helper is missing on the `skein/` side, port a
  copy or (preferred) consolidate to one shared module — additively, without touching
  the hashing. Resolve this in implementation (§10 #1).

---

## 8. Reuse boundary

- **Reuse as-is (server-side, already correct):** the ingress write server (9101) —
  except the one-line dangling relaxation of §5.3 (accept-and-flag) — the web read
  server (9001) — except the `created_by` byline must be relabeled as a claim (§6.1,
  display phase) — and the Merkle/manifest/signing libraries. Never had the fat-client
  problem.
- **Rewrite thin (the authoring half):** the publish path. `skein_next`'s
  `publish.py`/`collect_publish_set`/`_closed_threads` read the `.skein-next` store
  in-process; reuse the batch-build/sign/wire logic but **re-point "read what to
  publish" at the working server's own store** (the route runs *inside* the 8001
  server, so it reads its store directly — server-side, not fat-client), and add the
  author-declared/proposer/linter behaviour (§5).
- **Drop:** `skein_next`'s local authoring verbs and its store as an author source of
  truth.

---

## 9. Test design (RSP phase-2 contract — written before implementation)

The contract, specific to the decisions above. Each is a test to make green;
failure-injection tests assert the check FIRES (a clean path never proves a gate).
Two surfaces need an **independent shadow** (a from-scratch second implementation, no
shared imports): the Merkle root and manifest membership.

**A. Author-declared assembly (the core invariant):**
- A1. An explicit `manifest` publishes **exactly** the declared folios+threads — no
  auto-added edge, no silent drop. (Inject an extra reachable edge NOT declared →
  assert it does NOT travel.)
- A2. The `dry_run` proposal returns the reachability set; the real call uses the
  **declared** set, not the proposal. (Edit the proposal → assert the sent set == the
  edited set, not the proposal.)
- A3. `select` → proposal is the current `_closed_threads` result over the selection
  (a behavioural pin so the demotion-to-suggestion did not change the reachability
  math).

**B. Linter (advisory — fires but never blocks):**
- B1. Dangling declared edge → a `dangling` warning AND the publish proceeds AND the
  edge travels. (Failure-injection: assert the warning fires; regression: assert it
  did not block.)
- B2. Slug endpoint → a `slug-endpoint` warning; still publishes.
- B3. `status`/`tag` self-loop declared → a `looks-local` warning; still publishes
  (the "closed on the public station" path, §5.4).
- B4. A clean structural set → **zero** warnings (no false positives).

**C. Physics floor (fail closed):**
- C1. A folio/thread whose bytes do not reproduce its content hash → rejected before
  signing. (Injection.)
- C2. Manifest root recomputes from `leaf_list`; a tampered `leaf_list` fails
  membership. (Injection + the membership shadow.)
- C3. Merkle root over a known constituent set matches the **independent shadow**
  RFC-6962 implementation (sorted+deduped leaves, unwrapped odd-node promotion, empty
  rejected).

**D. Signing:**
- D1. The signed descriptor verifies via `verify_wire_manifest`; a wrong-profile or
  tampered bundle is rejected.
- D2. No `token` → the route refuses to sign/send (fail closed).
- D3. `dry_run` signs and sends **nothing** (no Rekor entry) — asserted via a fake
  signer that records calls.
- D4. Over-`MAX_LEAVES` fails **before** the ceremony.

**E. Receiver relaxation (additive, regression-guarded):**
- E1. A batch with a dangling edge is **accepted-and-flagged** by the ingress (was:
  rejected "dangling endpoint"). The edge's hash is a valid signed leaf; membership
  passes; it is stored.
- E2. A non-dangling batch behaves **byte-identically** to today (the closed-graph
  happy path is unchanged apart from the widening).

**F. API-first / thin-CLI:**
- F1. `POST /publish` publishes end-to-end via curl alone (no CLI). Round-trip against
  a locally-run ingress: login-token → POST → ack → the content is queryable on the
  station's read surface.
- F2. The CLI `publish` verb routes through the **same** server path (assert the CLI
  hits `/publish`, does not assemble/sign in the client — a thin-client guard).

**Fell the test design (phase 4) before implementing.** Hollow tests and fake-coverage
skips are load-bearing bugs.

---

## 10. Decided vs open

**Decided (this gate):**
1. Publish is a curlable local API route on the working 8001 server; the CLI verb
   wraps it (§4).
2. Admission is author-declared; type-classification is demoted to an advisory linter;
   physics (integrity) is the only veto (§5).
3. Dangling is allowed-and-warned on the author side and accept-and-flagged on the
   receiver; operator-side bans are out of scope (§5.3).
4. Signing is the existing 1-click login → token → route; no credential lifecycle;
   reuse `skein_next`'s Sigstore machinery unchanged (§6).
5. Reuse the ingress + crypto as-is; rewrite only the authoring half thin; build on the
   working skein, never on the abandoned tree (§2, §8).
6. `compute_thread_hash` / `canon.py` hashing untouched; additive to the running
   system (§7).
7. A manifest signature is **destination-agnostic** — no station/origin/audience
   binding; re-hosting signed content anywhere is intended federation, not a leak
   (§6.1).
8. The **signed identity is the only authenticated actor**; `created_by` is an
   unverified claim and must not be rendered as verified authorship (§6.1).
9. The publish route builds only via the all-up `assemble_signed_batch`
   (physics→leaves→sign→attach), so the signature can never be forgotten and physics
   runs before the Sigstore ceremony; `physics_check` is **total** (typed
   `PhysicsError` on any malformed row, incl. a non-mapping row) and `lint_declared_set`
   is **total** (advisory, never raises), so the route fails closed with a 4xx, not a
   500 (§4.3, §5.5). Contract expanded to 30 tests incl. the manifest-membership shadow,
   odd-node Merkle counts, and physics/linter totality (§9).

**Open (resolve in implementation or a follow-up):**
1. **Fork consolidation.** `canon.py`/`signing.py` are byte-identical forks; is a
   `sign.py`-equivalent manifest builder present on the `skein/` side, or ported, or
   consolidated to one shared module? Preferred: one shared module, additive, hashing
   untouched. Confirm at implementation.
2. **A further gate on the publish route** beyond "needs a valid token": the route
   relays a signed batch to any http(s) URL the caller names (destination-agnostic by
   design, §6.1) — SSRF-by-design, bounded by the token-gate (no token → 400, dry-run →
   no post) and an http(s)-scheme check (a `file://`/schemeless `to` is a pre-sign 4xx).
   A stricter destination allowlist is the open call; default is none.
3. **Signer-scoped `status_of`** for competing statuses on a shared hash (§5.4) — a
   later display phase.
4. **kasq** (fresh-db `thread_id`-PK) — re-confirmed **not** a Phase-4 blocker (the
   bridge re-resolves endpoints; and Phase 4 reads the working store, which is
   post-swap). Still worth doing for legacy-store health, separately.
5. Whether an **unsigned** publish posture is offered at all, or signing is mandatory
   (§4.2 `token` handling).

---

## 11. RSP status

- Phase 1 (Research + this gate): research done first-hand both sides; **in
  two-genotype fell** (opus + codex).
- Phase 2 (Test contract, §9): the pure/portable core is drafted as
  `tests/test_phase4_publish.py` and is **green (12/12)** — proves the model is
  buildable (proposer, linter, physics floor, manifest + independent Merkle shadow,
  signing with an injected fake signer). Route / CLI / ingress-relax legs pending.
- Phase 3 (Implementation): **BUILT** — `skein/publish.py` (core), the `POST /publish`
  route (`skein/routes.py`, served at `/skein/publish`), the thin `skein publish` CLI
  verb, the additive `get_thread_by_hash`, and the ingress accept-and-flag relaxation
  (`skein_next/ingress.py`). Additive (new files + additive methods + one route + the
  ingress widening); full suites green.
  The general `select` → arbitrary reachability proposer (§4.2/§5.2) remains deferred;
  `propose_reachable` exists + is unit-tested but is not wired to the route. The named
  site case is now first-class (issue-20260720-wvvk): `skein publish --site SITE`
  declares that workbench site's current non-site heads (or an explicit subset), adds a
  stable `type=site` anchor and deterministic `within` edges, and carries the validated
  `site_slugs` claim. Its `dry_run` returns those exact identities without persisting,
  signing, or sending; a real authenticated send persists the anchor/memberships through
  the normal workbench writers before signing at the boundary. Ordinary explicit
  manifests remain supported and may carry validated `site_slugs` directly. Also
  deferred: the display-phase `created_by` relabel (§6.1).
- Phases 4–5 (Fell rounds + hardening: knuth / oracle / gremlin): per the RSP playbook.
