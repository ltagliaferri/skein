# encoji codec — Phase 1: alphabet curation + research (rev 6)

**RSP:** encoji (encode + emoji) — the reversible emoji codec. Phase 1 of the Rock-Solid Primitive discipline (research → test design → implementation → hardening), per playbook-20251218-pb2o.
**Filed by:** the stapler jockeys-0529, pair with Patrick 2026-05-29.
**Brief:** brief-20260529-nj2k. **Inherits pinned constraints from:** finding-20260528-brgy (addressing rev 3, fell-clean), brief-20260528-1wj3 (rev 5).
**Name:** "encoji" adopted this session.
**Deliverable:** the curated 1024-glyph alphabet (reproducible, checksummed) + the edge-case list Phase 2 must test.

**Rev 2 changes** (fold-in of fell r1 [spool 46eeaae9] + external review from knuth, inventor, GPT-5.5; Kimi spin failed to launch — "LLM not set"):
- Re-sorted the alphabet by (age, codepoint) so the cluster range 0–783 is entirely Unicode age ≤ 9.0 (was codepoint-sort, which leaked age-13 glyphs like 🤌 into clusters). **New checksum.**
- Added: normalization-pass spec, cross-version brand-pool rule, explicit positional parse algorithm, control-range structural invariants + sub-role rejection (fell BLOCK B1), big-endian identity packing pinned, NFC/NFKC closure invariant, per-version manifest schema, station-badge concept, malformed-Unicode + empty-input + default-ignorable test cases.
- Fixed: modifier-base count (112, not 98); generator now emits the `encoji-*` filenames the artifacts are referenced by.

**Rev 3 changes** (fold-in of fell r2 [spool cc33b3d4]; both prose-only, checksum `db9eeaf6…` unchanged):
- Normalization (finding 3): U+20E3 keycap and the variation selectors are now listed as *explicit* strips, not lumped under `Default_Ignorable` (20E3 is an enclosing mark, category Me — not DI); added the note that computing the DI set needs `DerivedCoreProperties.txt` (not vendored) → enumerate it in the manifest.
- Section C: added the empty-station and route-present-minimum-length rejects (C7b); named the pre-delimited-stream precondition.

**Rev 4 changes** (fold-in of fell r3 [spool 38235346]; one-line prose fix, checksum unchanged): corrected the DI-grouping sentence in finding 3 — variation selectors ARE Default_Ignorable; the not-DI strips are the keycap (Me) and skin-tone modifiers (Sk).

**Rev 5 changes** (pair decision 2026-05-29; data checksum `db9eeaf6…` unchanged): pinned an **extension sentinel** (finding 13) — ➿ U+27BF, a glyph *outside* the 1024 data alphabet — for forward-compatible optional extensions. Updated the valid-codepoint set (finding 3), the manifest schema (finding 9), and added sentinel test cases (section G).

**Rev 6 changes** (fold-in of fell r5 [spool e2e4c71a]; prose-only, checksum unchanged): reconciled the sentinel addition with the rest — replaced the now-contradictory escape notion in finding 11 with a forward-pointer to finding 13; scoped finding 12's right-anchored helpers (badge/length) and the canonical re-encode (finding 3) to the *base* sub-stream when a sentinel is present; added the leading-sentinel reject test (G7) and the trailing-sentinel human-relay vector to the Phase 4 audit scope.

---

## Headline: 1024 single-codepoint emoji at Unicode ≤ 9.0 does not exist

The pinned spec asked for 1024 single-codepoint emoji, Unicode ≤ 9.0, default emoji presentation (no variation selectors), no ZWJ/modifiers. The real pool is **879** — a hard ceiling, not a target. Pool sizes (single-codepoint, `Emoji_Presentation=Yes`, no modifier/component):

    age ≤ 9.0 → 879   ≤10.0 → 935   ≤11.0 → 997   ≤12.0 → 1058   ≤13.0 → 1113   ≤14.0 → 1150   ≤15.0 → 1170

**Decision (pair, 2026-05-29):** raise the bound to **Unicode/Emoji 13.0 (2020)**. Pool = 1113, 89 slots of headroom, curate to 1024. Not a relitigation of the pinned design — the grammar, 5-emoji identity, positional decode, canonical encoder/decoder-rejection all stand; this corrects a factual impossibility, which is what Phase 1 is for. A forward-pointer was threaded onto finding-20260528-brgy so the superseded "≤ 9.0" line there isn't read in isolation.

---

## The alphabet (reproducible + checksummed)

**Canonical artifacts:** `docs/emoji-codec/` —
`encoji_alphabet.py` (deterministic generator = the spec of the alphabet), `encoji-alphabet.json` / `.txt` (full 1024-entry table), `encoji-dropped.txt` (the 89 exclusions with reasons), `inputs/` (vendored UCD 15.0.0 source — offline-reproducible). Reproduce: `cd docs/emoji-codec && python3 encoji_alphabet.py inputs .`

**Checksum:** sha256 of the canonical `index\tU+XXXX\n` table = `db9eeaf666177d145e7e061af02b13f104e135b067a427b4c3f851a1ffec13da`. Phase 3 must reproduce this exact digest; rebuilding from the vendored inputs reproduces it byte-for-byte.

**Pinned inputs:** UCD 15.0.0 emoji-data.txt + DerivedAge.txt (DerivedAge is monotonic, so age values are version-stable) + emoji-test.txt (group/subgroup annotation only).
**Candidate-pool filter:** single codepoint; `Emoji_Presentation=Yes` (renders as emoji with NO VS-16); NOT `Emoji_Modifier`, NOT `Emoji_Component`; age ≤ 13.0. → 1113.

### Code ranges (assignment = survivors sorted by (age, codepoint) → index 0..1023)

    0–783    cluster    2-char clusters over [a-z . -], index = c1*28 + c2 (28² = 784)
    784–811  singleton  one per cluster-alphabet char (forced boundary / final odd char), 28
    812–821  digit      digit terminators 0–9 (10)
    822–838  control    brand + route-subset + type-subset (17 codepoints); partition is Phase 3
    839–1023 reserved   future expansion (185); glyphs fixed now, semantics later

Cluster alphabet fixed as `abcdefghijklmnopqrstuvwxyz.-`. Verified: index 0 = cluster `aa`, 27 = `a-`, 28 = `ba`, 783 = `--`.

**Assignment ordering = (age, codepoint).** The index→glyph map is arbitrary for *correctness* (round-trip rides only on codepoint identity), so the ordering is free. Age-then-codepoint concentrates the oldest, most-universally-supported glyphs at the lowest indices. **Verified consequence: the cluster range 0–783 is entirely Unicode age ≤ 9.0** (index 0 = ⌚ age 1.1 … index 783 = 🦅 age 9.0; first age-10 glyph is at index 796). Because clusters carry the bulk of station encoding and the dominant visual mass of a station badge, **the cluster portion of any address renders on any Unicode 9.0+ platform.** Singletons/digits/control and the identity field (which draws from the full 1024 for the clean 2⁵⁰ packing) may include up to age-13 glyphs — those degrade to tofu on pre-2020 devices but still round-trip (decode is by codepoint, not pixels). The final 1024 holds 796 age ≤ 9.0 glyphs (the 89 drops removed many *old* glyphs), so 0–783 all-age≤9 is feasible (783 < 796) but the full active+control range 0–838 is not.

---

## Curation ruleset (1113 → 1024, exactly 89 removed)

Tone, confirmed with Patrick: **cleaned up, not sanitized** — iconic-edgy glyphs (💀 skull, 💣 bomb, 💩 poop) deliberately kept.

    distinctness   24  clock faces (U+1F550–U+1F567) — indistinguishable at glyph size
                    4  geometric size/UI near-duplicates (🔸🔹🔲🔳 — pure variants of kept glyphs)
    text-boundary  29  script-letterform boxes (🆎🔠🈁-style; Latin/Japanese, script-specific)
                   13  character-adjacent marks (❌❎❗❓❔❕➕➖➗⭕➰➿🔟)
    off-putting     9  prohibition signs (⛔🚫🚭🚯🚱🚳🚷📵🔞)
                    8  gross/weapon/offensive (🤮🔫🪓🔪🧨🖕🚬🤬)
                    2  gross relief-valve (🦠🩸 — absorbed the count; 💩 kept)

**Why "character-adjacent" is functional, not taste:** an encoji address is text + emoji side by side; a `➖` or `❗` emoji can be misread as literal address punctuation (`::`, `.`, `-`). Removing punctuation-shaped emoji keeps the emoji layer visually separable from the text layer. Full per-glyph drop list: `docs/emoji-codec/encoji-dropped.txt`.

---

## Key findings

### 1. encoji is an opaque fingerprint, not human-readable text — and digit glyphs are infeasible
The only Unicode "digit emoji" are keycap sequences (`0️⃣` = U+0030 U+FE0F U+20E3, three codepoints with a VS and a combining mark). Under single-codepoint/no-VS they cannot exist. So the digit slots — and the whole alphabet — are arbitrary opaque tokens. **Asymmetry to lean into (inventor):** the *station* segment is deterministic from the station name (everyone who knows `auth-service` sees the same prefix — a recognizable *badge*), while the *identity* 5 are hash-derived and opaque (the *fingerprint*). UI guidance: render station glyphs dim, identity glyphs bright — "the dim part says where, the bright part says which."

### 2. Skin tone is display-only and unencodable (the structural win)
A modifier-base emoji in bare form is one codepoint and renders neutral (yellow) by default; skin tone is an *appended* modifier (U+1F3FB–1F3FF), which is `Emoji_Component` and can never be an alphabet member. So **skin tone carries zero bits — unencodable.** This removed the entire human-form neutrality problem without dropping a single human glyph: **112** skin-tone-able (`Emoji_Modifier_Base`) glyphs remain (of 129 People & Body), all neutral as bare codepoints. Display layers may skin bare bases (cosmetic, decodes identically — skin tone is to encoji what font is to text).

### 3. The decoder needs an explicit normalization pass (fold-in: GPT-5.5, inventor, fell A2)
Because the alphabet is bare single codepoints, a real-world paste may carry cosmetic/invisible baggage (VS-16/VS-15, ZWJ, skin-tone, keycap, BOM, bidi controls, **tag characters** — note 🏴 can grow a tag-sequence subdivision flag, a spoof vector). Pin **one exact rule and one pipeline order:**
- `normalize(stream)` strips a fixed, enumerated cosmetic/invisible set: (a) variation selectors **U+FE0E / U+FE0F**; (b) the combining enclosing **keycap U+20E3**; (c) emoji **skin-tone modifiers U+1F3FB–1F3FF**; (d) all **`Default_Ignorable_Code_Point`** characters (ZWJ 200D, BOM/ZWNBSP FEFF, word-joiner 2060, CGJ 034F, zero-width 200B–200F, bidi controls 202A–202E / 2066–2069, tag chars E0020–E007F, soft hyphen 00AD, …). Groups (b) keycap and (c) skin-tone modifiers are genuinely **not** Default_Ignorable (U+20E3 is an enclosing mark, category Me; skin-tone modifiers are category Sk), so a naïve "strip all DI" misses them; group (a) variation selectors **are** themselves DI but are pinned explicitly for robustness. Computing the DI set requires UCD `DerivedCoreProperties.txt`, which is **not** among the three vendored inputs — Phase 2/3 MUST vendor it or enumerate the exact DI codepoint set in the manifest's `normalization_profile` (don't leave "all DI" as an unpinned dependency).
- After normalization, **any** codepoint not in the valid set → reject (no silent acceptance, no homoglyph substitution). The valid set is the **1024 data glyphs ∪ {extension sentinel U+27BF}** (finding 13); the sentinel is structural, never data, and **must NOT be stripped** by normalize (it is not a default-ignorable, and stripping it would corrupt framing).
- **Pipeline order is `normalize → split at first extension sentinel (finding 13) → [base: right-anchor peel → canonical re-encode compare] → reject/flag the extension`** — stripping changes glyph offsets and the decode is positional, so normalize MUST precede the split and peel. The canonical re-encode compares against the **base** sub-stream only: a canonical re-encode never emits the sentinel, so comparing the full stream would always fail when an extension is present. (Test: a stray modifier stuck to an *identity* glyph; naive order miscounts the peel.)
- Canonical output is always bare. Public API is `decode(normalize(input))`; skipping normalize is non-conformant.

### 4. UTF-16 surrogate segmentation — the biggest implementation landmine
**977 of 1024 glyphs are astral (U+10000+); 47 BMP.** In UTF-16-indexed languages (JS especially) `.length`, `str[i]`, `.split('')`, non-`/u` regex split glyphs mid-surrogate. The decoder MUST iterate by codepoint (`Array.from`/`for…of`/Python str/Rust `.chars()`).

### 5. One codepoint == one grapheme (deliberate, tractable)
No ZWJ/VS/modifiers ⇒ every glyph is one codepoint and one grapheme; codepoint iteration equals grapheme iteration. The decoder relies on this and (per finding 3) enforces it.

### 6. Rendering fallback is display-only — round-trip survives tofu
Decode is by codepoint; copy-paste preserves codepoints even when a device shows □. An un-renderable address still decodes. This is the safety net under the age-13 decision.

### 7. Identity = exactly 2⁵⁰; pin big-endian base-1024 (fold-in: knuth)
1024⁵ = 2⁵⁰ exactly → 10 bits/glyph, no modulo bias. **Pin now (not Phase 3):** big-endian base-1024 — `value = i₄·1024⁴ + … + i₀`, where i₄ is the **leftmost** identity glyph (most significant). Pinning the choice now lets Phase 2 write non-parameterized tests. **Stronger constraint than "changes the checksum" (fell M2):** because identity glyph = `alphabet[index]`, any future glyph-table reorder doesn't merely rebuild the checksum — it **invalidates every previously-issued identity address.** Reordering is therefore strictly a new *version*, never an in-place change.

### 8. NFC/NFKC closure is an invariant (fold-in: knuth, fell-confirmed)
All 1024 glyphs satisfy NFC == NFD == NFKC == self (verified). State as a **standing invariant**: the encoji alphabet is closed under Unicode normalization, and any future alphabet revision MUST preserve it — so platform normalization can never corrupt an address.

### 9. Versioning + the cross-version brand-pool rule (fold-in: knuth, GPT-5.5, inventor)
The brand glyph (position 0) doubles as a version magic-number (classic first-byte dispatch, like PNG/PDF). **Pin the cross-version rule:** all future encoji versions MUST draw their brand glyph from the v1 control-range *codepoints* (indices 822–838), even if the rest of the alphabet changes. Then any decoder can classify glyph 0: in the brand pool but unknown → "unknown encoji version" (distinct error); in the alphabet but not the brand pool → structurally invalid (no brand); not in the alphabet → "not encoji". Caps the scheme at ≤17 versions sharing the pool — ample. Cheaper and cleaner than a separate `[brand][version]` two-glyph header (which taxes every address forever for a v2 that may never come).

The system of record is a **per-version codec manifest** (fold-in: GPT-5.5 idea, inventor): `{version, brand_codepoint, alphabet_sha256, codepoint_table, identity_length, control_partition, normalization_profile, packing_order, extension_sentinel, prior_brand_registry}`. `identity_length` is a first-class field (so a future v2 could choose 6 emoji without special-casing). This operationalizes "alphabet as versioned data, not hardcoded logic."

### 10. Control-range structural invariants (fold-in: knuth, GPT-5.5; resolves fell BLOCK B1)
- The control-range **boundary (indices 822–838) is permanent** across versions; the **partition** of those 17 codepoints into brand / type / route subsets is a *per-version* decision (Phase 3 sets v1's).
- **The brand / type / route subsets MUST be mutually disjoint** — this is what makes route-presence and slot-roles unambiguously detectable. Pin it as an invariant.
- Station-position glyphs MUST be in **0–821**; any glyph ≥ 822 (control *or* reserved) in a station position is a parse error (tightens the old "reject control mid-station" to also reject reserved).

### 11. Residuals (flagged, not blocking)
- **Homoglyph / confusable audit of the active+control subset (0–838) is a named pre-lock/Phase-4 action** (fold-in: GPT-5.5, inventor). Threat model is **human relay/phishing** (an address screenshot that a person mis-transcribes, or two glyphs a casual viewer confuses), NOT decode (decode is codepoint-exact). I did not perform a full *visual* audit — that needs rendering across platforms, which this session can't do. Phase 4 should run it on the glyphs humans actually relay and treat "easy to mis-transcribe at small size / lookalike to ASCII or to another glyph" as a hardening criterion. Add to that audit's scope (fell r5): a **trailing sentinel + arbitrary appended glyphs on an otherwise-valid base** — a longer string that still resolves to the same base (a human-relay confusion vector). And pin for the addressing grammar that "base resolved + extension unsupported" must NOT be treated as byte-equal to a bare base.
- **🎌 CROSSED FLAGS kept** — a national-flag-adjacent symbol (other Flags-group members 🏁🏴🚩 are neutral); trivially droppable + backfillable under a stricter neutrality pass.
- **Extension/escape:** now pinned — see finding 13 (extension sentinel ➿ U+27BF). This *supersedes* an earlier notion of using a reserved-*range* glyph as the marker: a reserved-range glyph (839–1023) is inside the 1024 and identity-reachable, so it cannot be an unambiguous marker, and (per the positional decode) v1 would consume it as identity rather than detect it. The forward base-resolvability property is reserve-now-or-never — a version bump can't provide it — which is why finding 13 pins the sentinel now.
- **Control-range glyph→meaning table** (brand/type/route, the deliberate yarn 🧶 brand) — Phase 3.

### 12. Free capabilities that fall out of the design (fold-in: inventor)
- **Station badge:** `badge(address) = address minus the right-hand identity 5` is a canonical per-station fingerprint (same for all folios in a station). Free; useful for UI grouping.
- **First-codepoint sniffer:** `codepoint(stream[0]) ∈ brand_pool` is an O(1) "is this an encoji address?" check on raw text — enables paste-detection without loading the full codec.
- **Length pre-check:** minimum length = brand(1) + station(≥1) + type(1) + identity(5) = 8 (route-less); shorter is immediately malformed.
- **All three helpers operate on the BASE portion** (the part before the first extension sentinel, finding 13) when a sentinel is present: `badge()` strips the identity-5 from the base (not from the extension), and the length pre-check applies to the base sub-stream (whole-stream length is necessary but not sufficient — a sentinel near the front can leave a base shorter than 8).

### 13. Extension sentinel — forward-compatibility, pinned now
A single reserved **extension sentinel** is pinned so addresses can carry optional extensions later **without a version bump**, and — the property that must be reserved now or never — so a decoder shipped **today** can still resolve the **base** of a *future* extended address (a version bump cannot do this: an old client hitting an unknown brand rejects the whole stream).

**Sentinel = ➿ U+27BF DOUBLE CURLY LOOP.** Chosen because it is **outside the 1024 data alphabet** (it was one of the 89 glyphs dropped *from data* — dropped for being symbol-like, which is exactly the right quality for a structural marker), Unicode **age 6.0** (renders on virtually every platform), single codepoint, default presentation, and a loop of thread that pairs with the 🧶 brand.

**Why outside-the-1024 is essential (not incidental):** identity draws from the full 1024 for the clean 2⁵⁰ packing, so *any* in-alphabet glyph can occur inside an identity by chance and therefore cannot serve as a unique scannable marker. A sentinel outside the 1024 can never appear in station or identity, so "the sentinel appears ⇒ everything after it is the extension" is unambiguous at any position. (This is why a reserved-*range* glyph, 839–1023, would NOT work — that range is part of the 1024 and is identity-reachable.)

**Rules (pinned v1):**
- Valid codepoint set = the 1024 data glyphs ∪ {U+27BF}. The sentinel is never a data glyph (never station, never identity).
- The **first** occurrence of the sentinel delimits the stream: the part **before** it is a normal base address (decoded by the usual count-from-the-end — identity = the 5 glyphs immediately preceding the sentinel); the part **after** it is the extension payload. The base portion cannot contain the sentinel by construction, so the first occurrence is unambiguous.
- v1 decoders on encountering the sentinel **still resolve the base address** and treat the extension as unsupported — **reject-the-extension-with-specific-error, NOT reject-the-whole-address.** This is the durable forward-compat property.
- The sentinel is structural: `normalize()` must not strip it (finding 3).
- The data checksum `db9eeaf6…` is unchanged — the sentinel is a separate `extension_sentinel` manifest field (finding 9), not part of the data table.
- The **extension payload grammar is undefined in v1** (no extension exists yet). v1 pins only the sentinel glyph, the split rule, and base-resolvability; the payload — including any need to escape the sentinel within it — is defined if/when the first extension is specified.

---

## Phase 2 test contract — the edge-case gauntlet

**A. Encoder canonical form (station → emoji)**
A1 single char→singleton; A2 even all-letter→all clusters; A3 odd→final singleton. A4 digit terminates current cluster: flush odd leftover as forced-boundary singleton, emit digit, resume (`abc3`→`ab`,`c`,`3`). A5 leading digit (`3dns`); A6 consecutive digits (`443`); A7 trailing digit (`v2`). A8 `.`/`-` are cluster chars: punycode `xn--…`→`xn`,`--`; leading/trailing/double dots+hyphens. A9 full punycode authority round-trip. A10 non-canonical authority (uppercase/IDN/percent-encoded host) → parse error (per finding-20260528-brgy). A11 max authority (253 chars) — count + perf.

**B. Decoder canonical-rejection (emoji → station)** — core contract
B1 decode each glyph by range, re-encode canonically, **reject if ≠ input**. B2 reject two adjacent singletons that should pair. B3 reject a singleton that's not a forced-boundary/final-odd. **B3a (knuth): reject adjacent fragments that decode to the same string but clustered differently than the canonical greedy encoder (`[singleton a][cluster .b]` for `a.b`, vs canonical `[cluster a.][singleton b]`).** B4 reject any non-alphabet codepoint. B5 the normalization pass (finding 3): strip the enumerated default-ignorables + skin-tone modifiers; reject all other non-alphabet; **test the pipeline order** (modifier on an identity glyph). B6 property: ∀ valid station s, decode(encode(s))==s; ∀ canonical stream, encode(decode(stream))==stream.

**C. Positional right-anchored decode (full stream)** — *Precondition:* the codec operates on an already-delimited address stream; locating address boundaries within surrounding text is the addressing grammar's job (aided by the first-codepoint sniffer, finding 12). **Parse is POSITIONAL, not range-scanning (knuth):** peel exactly `identity_length` from the right; do NOT scan for the first control glyph.
C1 layout `[brand][route?][station…][type][identity×5]`. C2 peel last 5 as identity (full 1024 range; may codepoint-collide with station/control glyphs — right-anchoring still resolves). C3 position −6 must be a **type-subset** control glyph — reject a non-control glyph there **and** reject a control glyph of the wrong sub-role (route/brand code in the type slot) [resolves BLOCK B1]. C4 station run must be glyphs **0–821**; reject any glyph ≥822 (control or reserved) mid-station. C5 route present **iff** a **route-subset** control glyph sits in the route position (right after brand); test present/absent, and reject a type/brand code in the route slot. C5a brand/route/type subsets are mutually disjoint (invariant). C6 glyph 0 must be a **brand-pool** codepoint; unknown brand → "unknown version"; non-brand alphabet glyph → "no brand / invalid"; test both. C7 minimum length: route-less = 8 (brand + station≥1 + type + identity×5), **route-present = 9**; reject shorter; **C7a explicit empty / zero-length input** (fell M4); **C7b station run must be ≥ 1 glyph — reject empty station** (else an 8-glyph `[brand][route][type][identity×5]` decodes to an empty station with nothing rejecting it — fell r2). C8 identity is exactly 5 — 4 or 6 must fail structural validation, not silently mis-parse.

**D. Identity / 50-bit / collision**
D1 50 bits ↔ 5 glyphs, big-endian base-1024 (i₄ leftmost = MSB), pinned; test both directions, no modulo bias. D2 short-hash is station-scoped (decode station → station resolves prefix → full digest from the folio). D3 collision (two folios share the 50-bit hash) → emoji ambiguous → fall back to text full-hash; test detection + fallback. D4 resolver detect-and-error on ambiguity (return colliding full hashes, never silently pick) — per finding-20260528-brgy.

**E. Unicode / encoding gauntlet**
E1 surrogate pairs (977/1024 astral): a UTF-16-code-unit-indexed parse must be rejected/avoided; decode iterates by codepoint. E2 NFC/NFD/NFKC: glyphs non-decomposable; normalization of a valid stream doesn't alter it; stray combining marks → strip-or-reject per finding 3. E3 invisible/zero-width/bidi/tag characters before, **between, and at the edges of** glyphs → stripped by normalize, then any residual non-alphabet → reject (fold-in: GPT-5.5). **E4 malformed encodings (fell A1): lone/unpaired surrogate (e.g. U+D800), overlong UTF-8, invalid continuation bytes → reject cleanly, never crash or mis-decode.** E5 byte round-trip (UTF-8 4 bytes/astral, UTF-16, UTF-32); copy-paste preserves codepoints. E6 bidi/RTL is display-only and must not reorder the codepoint sequence.

**F. Rendering / width fallback**
F1 un-renderable glyph (pre-2020 device) → tofu but codepoint preserved → decode succeeds (display ≠ decode). F2 font substitution → same codepoint → decodes. F3 terminal single/double-width — display only.

**G. Extension sentinel (finding 13)**
G1 sentinel-absent → normal decode unaffected. G2 sentinel-present → split at the **first** sentinel; the base (before it) decodes normally with identity = the 5 glyphs immediately preceding the sentinel. G3 **base remains resolvable** when the extension payload is unknown/unsupported (reject the extension, not the whole address). G4 the sentinel is in the valid set (1024 ∪ {U+27BF}) but **never** accepted as a station or identity glyph; a data glyph is never mistaken for the sentinel (by construction — it's outside the 1024). G5 `normalize()` does NOT strip the sentinel. G6 a sentinel as the very last glyph (empty extension) — define accept-with-empty-extension vs reject. G7 a **leading** sentinel (empty base) → rejects via the glyph-0 brand/sniffer check (U+27BF ∉ brand pool).

---

## What Phase 1 does NOT decide
- Module home (knurl vs skein) — station-work call; Phase 1 is a finding + spec artifacts, no module code.
- Control-range glyph→meaning partition (brand/type/route; the 🧶 brand) — Phase 3 (within the pinned invariants of finding 10).
- The full per-version manifest field encoding — Phase 3 (the schema is pinned in finding 9).
- The `::` address grammar — sibling RSP (addressing).
- The **extension payload grammar** — undefined in v1 by design (finding 13); specified if/when a first extension is needed. v1 pins only the sentinel glyph, the split rule, and base-resolvability.
- **Now pinned (were open in rev 1):** big-endian base-1024 packing (finding 7); the normalization rule + pipeline order (finding 3); NFC/NFKC closure invariant (finding 8); cross-version brand-pool + control-range invariants (findings 9–10); the extension sentinel ➿ U+27BF + split rule + base-resolvability (finding 13).

## Phase 1 → Phase 2 handoff
**Phase 1 is FELL-CLEAN through rev 6** (fell convergence r1→r6: BLOCK+3 actionable → 2 actionable → 1 minor → clean@r4; extension sentinel added rev 5 → reconciled rev 6 → clean@r6, spool 8007208e). Phase 2 kickoff filed as **brief-20260529-rx21**: write the gauntlet above as executable property/unit tests against the pinned alphabet (checksum `db9eeaf6…`) — tests first, no implementation — then Phase 3 implementation, Phase 4 hardening (including the human-relay homoglyph audit, finding 11).

---
*Provenance: fell r1 = spool 46eeaae9 (verified all arithmetic/checksum/filter claims by re-execution; raised BLOCK B1 + A1/A2/A3 + minors, all folded into rev 2). fell r2 = spool cc33b3d4 (re-verified the new checksum + age-≤9 cluster invariant byte-for-byte, confirmed every r1 finding resolved, raised two prose-only actionables — 20E3/DI + empty-station reject — folded into rev 3). fell r3 = spool 38235346 (re-confirmed checksum + both fixes by re-execution; raised one minor prose imprecision in the DI-grouping sentence — fixed in rev 4). fell r4 = spool 6b943da7 → **FELL-CLEAN** (verified the corrected DI sentence + Unicode categories + unchanged checksum). Rev 5 then added the extension sentinel (finding 13, pair decision). fell r5 = spool e2e4c71a (confirmed U+27BF outside the 1024 + checksum unchanged + base-resolvability; flagged one MEDIUM contradiction with the old escape notion in finding 11 + three LOW reconciliation gaps — all folded into rev 6). fell r6 = spool 8007208e → **FELL-CLEAN** (all four r5 items resolved, no new contradiction, checksum unchanged). External pre-lock review: knuth (api_2026-05-29_14-51-58-471), inventor (api_2026-05-29_14-52-00-672), GPT-5.5 (codex-806cd6cb). Kimi spin failed to launch (LLM not set) — 3 of 4 external reads obtained; their findings converged, so coverage is adequate.*
