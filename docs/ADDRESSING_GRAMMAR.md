# Addressing grammar — refs, hashes, and stations, in one scheme

Status: design note for review. This is the current-truth addressing document
*for design intent* — the existing `::` grammar described here is live, but the
ref layer is not yet implemented (the gap, and the exact `address.py` edits it
needs, are in "The ref layer"). It consolidates a scattered,
partly-contradictory body of prior specs into one scheme and adds the ref layer
on top. Where a prior folio disagrees with this note, this note wins; the
superseded folios are listed at the end.

Parent: brief-20260626-fmfs (one content-addressed model). Sibling design:
the rev3 `::` grammar already live in `skein/address.py` — that module is the
ground truth for the existing grammar; this note references it rather than
re-transcribing it.

## The model in one paragraph

SKEIN addresses are petnames over content hashes. A folio's identity is its
`sha256::` content hash — global, verifiable, the same on every machine. A slug
(`brief-20260626-fmfs`) is a local human name that points at the *head* of a
lineage, exactly like a git branch points at a commit. Stations are named
namespaces — your local nicknames for places folios live. Naming is local and
convenient; identity is the hash and is global. This is the Zooko/petnames
answer the December 2025 naming research landed on, and it is already the shape
of the live grammar. We are extending it, not replacing it.

## The existing grammar (ground truth: `skein/address.py`)

Three type words, all resolving to a content hash today:

- `sha256::<64-hex>` — a bare full content hash. No station context; cascades
  locally-known stations. `hash::sha256::<64-hex>` is the explicit spelling.
- `alias::<name>::sha256::<digest>` — scoped to a station nicknamed `<name>` in
  your local registry (`~/.skein/stations.json`). The alias is local-only and
  never travels on the wire. `<name>::sha256::<digest>` is the shorthand.
- `web::<authority>::sha256::<digest>` — scoped to a station addressed by domain
  (or IP), resolved over HTTP. The authority is globally routable.

Any of these three hash-target forms may carry a verifier fragment
`#sha256::<64-hex>`; if present, the resolved object's content hash must equal it.
(On a ref target — added below — the same fragment warns instead of rejecting;
see "The freshness suffix".) Authorities are validated, never
rewritten (validate-not-convert). Short hashes (8–63 hex) require an explicit
station — including `web::`, where the live parser allows them (`allow_short`).
The rule is narrower than "short hashes never go remote": a *durable
cross-instance citation* should carry a full 64-hex digest, because a remote
can't safely disambiguate an 8-hex prefix and it's a ~2^16 birthday target. A
short hash with a station context is legal; a short hash as a durable shared
reference is the thing to avoid.

## The ref layer (new)

A `ref` production resolves a slug to the *head* of its lineage. The forms:

- `<slug>` — a bare slug. Means a **local** ref, current station only. `ref::`
  is implied. This is the everyday human default (`brief-20260626-fmfs`).
- `ref::<slug>` — the explicit spelling of the bare local form.
- `alias::<station>::ref::<slug>` — head of `<slug>` on a locally-known station.
- `<station>::ref::<slug>` — the alias shorthand, accepted by analogy with the
  hash shorthand `<name>::sha256::<digest>`. The ref-aware `_build_alias` (below)
  makes it parse for free; it is canonical-accepted, not rejected.
- `web::<authority>::ref::<slug>` — head of `<slug>` on a remote station.
- any of the above may carry `#sha256::<64-hex>` — see "The freshness suffix".

A `ref` target is conceptually parallel to a `sha256::<digest>` target — it
occupies the same slot after a station segment. But it is **not** a drop-in
substitution at the parser or data layer, and the implementation must not treat
it as one. Against the live `skein/address.py`, refs require these changes
(verified by the design fell, with exact line cites):

- **A new 1-segment production** for bare `<slug>`. `parse()` has no single-token
  form today — a bare slug currently falls through to `_require_arity(segments, 3)`
  and raises. Forms 3 and 4 (`alias::…::ref::…`, `web::…::ref::…`) *do* fit the
  existing 4-segment arity; forms 1 and 2 are genuinely new.
- **A `ref` dispatch before the bare-hash 2-segment branch.** `ref::<slug>` would
  otherwise hit the `<algo>::<digest>` rule, construct `Hash("ref", <slug>)`, and
  die as "unsupported hash algorithm: ref". A `first == "ref"` branch must precede
  it; the slug also isn't hex, so it can never be a `Hash`.
- **A target kind on the address.** `ParsedAddress` types every address as a
  `Hash` with `type ∈ {alias, web, hash}`. A ref needs a slug-carrying variant —
  a `Target = Hash | Ref` (or a `slug` field plus a `ref` type). `construct()`
  needs a ref case to round-trip.
- **Ref-aware builders.** `_build_alias` / `_build_web` hardcode segment 2 as the
  hash algorithm. They need a `segment == "ref"` branch that builds a ref target
  instead of calling `_make_folio`.
- **Add `ref` to `_RESERVED_ALIASES`.** It is not reserved today; reserving it
  only prevents a station being nicknamed `ref` (it does not, by itself, make any
  ref form parse — that is the dispatch work above).
- **A ref branch in `resolve()` and a slug→head method on `StationIndex`.**
  `resolve()` reads `parsed.folio.digest` and lengthens a short hash via
  `folios_with_prefix`; a ref target has a slug, not a digest, so it needs its own
  resolution path that asks the station for the head of `<slug>`, and the station
  protocol needs a slug→head lookup. (This list covers the parse/construct/data
  edits and the resolution edit; it is the whole address.py surface refs touch.)

Convenient reuse: the slug grammar below (`[a-z0-9-]`, start/end alphanumeric,
≤32) is byte-identical to the existing `_ALIAS_RE`, so the slug validator is the
alias validator. Round-trip and a canonical ref spelling must be specified when
this lands.

**Bare slug resolves local-only.** If the slug isn't a ref in the current
station, resolution fails — it does not silently reach into other stations and
guess. This is deliberate: it kills the cross-project mis-hit that the old
`project:folio` scheme allowed, and makes every cross-station reference explicit.
This is a **visible behavior change**: the legacy bare-ID path resolves the
current project first and then *cascades across all projects*; local-only shrinks
that reach to the current station, so a bare ID that used to find a sibling
project's folio by cascade will now fail. Reaching another station becomes
explicit (`alias::<station>::ref::<slug>`). The bare-slug production must land in
`address.py` **before** `address_legacy` is retired, or the everyday default
breaks in the gap.

`ref` is a reserved word once the change above lands: no station may be nicknamed
`ref`.

Why cross-station refs are coherent where cross-station short-hashes are not: a
remote can't lengthen your 8-hex prefix without your local context, but a slug
is the *remote's own* namespace. You ask the remote "what's the head of
`<slug>`?" and it answers from its own refs table — like resolving a branch on a
git remote. The answer is the station's word (see trust, below), but it is
always well-defined.

## Locality: a station is one of three things

`alias::<station>::ref::<slug>` does not tell you whether the station is on this
disk or across the country. The address form is identical in all cases; locality
is a property of the registry entry, not the address:

- a sibling project on this machine (filesystem station, a local path);
- another machine over ssh (filesystem station, transport ssh);
- another machine over HTTP (web station, a domain).

`web::<authority>::...` is the one inherently-remote *form*, because a domain is
inherently remote. But it is not the only trust-boundary crossing: an
`alias::<station>::...` can point over ssh to another machine too. So locality is
the registry entry's property, and "is this binding from my disk or another
machine?" is answered by the resolved station, not by the address word. The
common case — an agent reaching a sibling project on the same box — needs no
network at all: registry lookup plus a refs-table read.

## Resolution is not discovery

This grammar owns **resolution**: given an address, find the folio. It does not
own **discovery**: knowing there is anything out there to address. An agent boxed
in its project that doesn't know `speakbot` exists is a discovery problem —
solved by cross-station search, disk-walking, or crumbs — and is separate,
broader tooling work. The grammar *makes discovery expressible* (a discovered
hit must land in an address form that exists) but does not perform it.

## The freshness suffix

On a ref, `#sha256::<digest>` is a **freshness note**, not a pin: resolution
returns the current head and **warns** if the head has moved off the named
digest. It does not verify and does not reject — on drift you get the *new*
head's content plus a warning, never the digest you named. The reasoning: if you
wanted a frozen version you would have written the bare `sha256::<digest>`, which
can never drift. A ref is the living slug, so a ref-with-suffix means "give me
head, and tell me if it moved since I wrote this." To actually pin-or-fail,
re-resolve the bare `sha256::<digest>` — that path rejects on mismatch.

On a bare/`hash`/station-scoped hash address, the same fragment is a **verifier**
the resolved object must match (reject on mismatch). This is the footgun to call
out: the identical `#sha256::<digest>` bytes mean *reject* after a hash target and
*warn* after a ref target. Enforcement is determined by the left-hand production,
and tooling must surface which mode is active so an agent assembling addresses by
string ops cannot silently lose enforcement.

## Trust and display posture

Two things come back from a ref resolution and they have different trust:

- The folio **content** is hash-verifiable — recompute the canonical bytes,
  confirm the hash. True regardless of who served it.
- The **slug-to-head binding** is the serving station's unsigned word. Nothing
  cryptographic backs "we say `<slug>` currently heads at this hash." Same trust
  tier as a local alias-to-hash binding, just possibly over the network.

Display posture follows the trust boundary — and the boundary is **station
locality**, not the address word. The test is "did this binding come from my own
disk or from another machine?", which the resolved station answers:

- **Refs resolved against a local (this-disk) station** keep the head sha
  backstage. You see the slug; you trust your own disk.
- **Refs resolved against any non-local station surface the head sha
  prominently** — `web::<authority>`, and equally `alias::<station>` that points
  over ssh to another machine. The binding is that machine's unsigned word, so the
  sha is the one thing you can hold and verify. Surfacing it lets an agent capture
  a citation — `web::<authority>::ref::<slug>#sha256::<d>` — that re-resolves to
  live head and warns on drift.

What that captured citation does and does not give you: on re-resolution it warns
if the head moved off `<d>`, but it does **not** verify the new content against
`<d>` (see the freshness suffix — on drift you hold different content). To recover
the exact cited bytes you re-resolve the bare hash `web::<authority>::sha256::<d>`,
which depends on the remote still serving by-hash — a federation seam that is
deferred (see below). And on a *first* contact with no prior trusted `<d>`, the
remote supplies both the head hash and the content, so content-matches-hash is
self-consistent by construction; surfacing the sha helps a human spot-check the
binding across time, it is not a defense on first resolution.

A slug only ever travels namespaced by an asserting authority and resolves to
"what that authority currently says" — never as a global identity. Global
identity is always the hash.

## Slug form and collision resistance

A slug must be a legal token in the grammar: lowercase `[a-z0-9-]`, start and end
alphanumeric, ≤32 chars. The type prefix (`brief-`, `finding-`, …) and date stay
— the prefix tells you what a thing is at a glance, the date shards the collision
space and aids reading.

Requirement: a slug must be **locally collision-free**. It need not be globally
unique — identity is the hash, and on the mesh a slug travels namespaced by its
authority, so two stations independently minting the same slug is fine; the
authority segment distinguishes them.

Target form: `type-YYYYMMDD-<6 lowercase-alnum>`, drawn from a crypto-random
source, with **check-and-retry at mint** so the result is *guaranteed* unique in
the store, not merely probable. Today minting is `type-YYYYMMDD-<4-char>` drawn
from `random`, with no uniqueness check — mint-and-hope. The widening cuts retry
frequency and federation-merge clashes; the check-and-retry is what makes it
collision-free. (Implementing the mint change is a separate follow-on with its
own review; this note fixes the requirement and the form.)

Resolver behavior on a same-station slug collision **today**, before the mint
change lands: refuse, do not guess. If a slug resolves to more than one lineage
head in a station, `ref::<slug>` raises rather than picking one — the same posture
as an ambiguous short hash. Ref resolution is only fully well-defined once
check-and-retry guarantees within-station uniqueness; until then the rare
mint-and-hope collision is an explicit error, never a silent wrong head. (This
sharpens the parent brief's "slug-as-ref works regardless of minting quality":
the mechanism works, but it is ambiguous-and-refuses on the rare collision until
hardening ships.)

## Edit rule

Folio type lives in the slug prefix and in the hashed `type` field, never in the
hash address (the grammar deliberately carries no type). To keep slug-type and
field-type from ever diverging: **type changes are disallowed on edit.** A
different type is a different lineage.

## Reconciliation — how the prior contradictions resolve

The readers surfaced ~90 addressing folios that disagree. The disagreements
resolve by what actually shipped and by the most recent decision:

- **Drop human IDs, hash-only** (finding-20260527-rh29, May) vs **keep slugs,
  wire hash alongside** (brief-20260626-1d7a, June). The recent one wins: slugs
  stay, refs are first-class, the hash is identity underneath. Note this reverses
  *rh29's whole identity stance*, not just the headline — it also demoted the type
  prefix to metadata (we keep it in the slug) and deferred standalone `hash::` to
  v1 (the live code ships it now). rh29's **grammar and station-type model
  survive**, but the ground truth for the live `::` grammar is brgy / ztp7 /
  `address.py`, not rh29.
- **Slash `project/folio`** (brief-20251226-5gas) vs **typed `::` grammar**
  (rh29). The `::` grammar shipped; the slash form is dead.
- **Aliases on the wire with `@peer` hints** (brief-20251228-vkg4) vs
  **aliases local-only, never on the wire** (rh29). Local-only shipped and wins.
- **Keep single-colon alongside** (brief-20260626-1d7a: "wired in ALONGSIDE the
  existing single-colon project:id scheme — do not replace it") vs **one model,
  not two** (parent brief-20260626-fmfs). These are not in conflict: 1d7a's
  "do not replace" is an *additive Phase-1 transition constraint*; the end-state
  retirement of single-colon is fmfs's one-model goal. So this note declares the
  end-state, and the cutover is sequenced (see Deferred) — single-colon stays live
  until its callers migrate.
- **Bare-ID cascade** (legacy: current project, then cascade across all projects)
  vs **bare slug local-only** (this note). We take local-only — but own it as a
  visible behavior change: bare-ID reach shrinks to the current station (see "Bare
  slug resolves local-only").
- Interskein over-rotated to surfacing sha everywhere; the posture here is
  slug-by-default, sha surfaced only across a trust boundary (any non-local
  station, ssh as well as web).

## Deferred / open (decide against real code, not here)

- **Durable station identity when a domain moves** — the operator-rooted form
  rh29 sketched (`oidc::<issuer>::<subject>::…`) is the intended future answer.
  Deferred to the mesh phase; not specified now.
- **The remote HTTP path convention** for `web::<authority>::ref::<slug>` — the
  exact origin endpoint shape is an undecided federation seam; settle it when the
  read protocol is built.
- **Best-effort cascade across known stations** — possible as an explicit opt-in
  later (bare slug stays local-only by default); not a default, because slugs
  collide across stations and silent wrong hits are worse than a clean miss.
- **The `address_legacy` cutover** — the single-colon `project:folio` parser
  (used by `client/cli.py` and `skein/routes.py`) is declared retired *as the
  end-state*; it stays live until those callers migrate. Sequencing constraint:
  the bare-slug production must land in `address.py` before `address_legacy` is
  removed, or the everyday default breaks in the gap. The actual swap is Phase 1
  implementation work.
- **Cross-project search / index** (from brief-20251227-aiek: `skein find --all`,
  a fast ID index) and **federation read-protocol questions** (from
  brief-20251228-vkg4: caching/staleness, hint-spoofing of the slug→head binding,
  fallback, error envelopes) — these ride along in folios this note supersedes for
  *syntax*, but they are real, unanswered, and out of scope here. Re-home them to a
  discovery follow-up and a federation/read-protocol brief so they are not lost.

## Superseded by this note

Superseding here is scoped to **addressing/naming syntax** — not to any
non-syntax obligations a folio also carries (those are re-homed; see Deferred).
The status changes are an action item: this note cannot itself flip a folio's
SKEIN status, so until someone runs the close/supersede commands these folios
still read as open.

Mark superseded-by this document, syntax only:
- brief-20251226-5gas (slash syntax; already closed). Its five-field
  `content_hash` set survives via fmfs/brgy — retire the addressing, not that.
- brief-20251227-aiek (slash Phase 1). Its cross-project search/index deliverables
  are re-homed, not retired.
- brief-20251228-vkg4 (single-colon hint protocol). Its caching/spoofing/fallback
  questions are re-homed to the federation/read-protocol work.
- the hash-only-identity stance of finding-20260527-rh29 (its `::` grammar and
  station-type model survive; its identity stance — drop human IDs, demote the
  type prefix, defer standalone hash — is reversed/overtaken).

Annotate, do not retire — these are ground truth for the existing `::` grammar
but contain one stance this note overrides:
- finding-20260528-brgy and brief-20260528-1wj3 say "no human folio_id / no
  display-handle / slugs not in addresses." Their **hash-as-identity** stance is
  correct and preserved. But their "no handle in addresses" position is overridden
  by the parent fmfs slug-as-ref decision: a slug is a *local head-pointer ref*,
  not a second identity, and `ref::<slug>` carries it in the address as a
  convenience over the hash — it does not reopen a second global identity. Note
  this on both folios so they are not read as forbidding the ref layer.
- finding-20260529-qlsx / -ztp7 / -ahbi remain valid as-is and are ground truth.
