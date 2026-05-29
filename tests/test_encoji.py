"""
Phase 2 test contract for the encoji codec (encode + emoji).

RSP: encoji (brief skein:brief-20260529-rx21). Design source, fell-clean:
skein:finding-20260529-uyt5 (Phase 1, rev 6) — the curated 1024-glyph alphabet,
the curation ruleset, the decode pipeline, and the edge-case gauntlet (sections
A-G) that this file turns into executable tests. Contract decisions that Phase 1
left open (and that Phase 2 must pin so the tests are runnable) are recorded in
skein:finding-20260529-dk67, reconciled with fell-r1 in skein:finding-20260529-*.

This file is the CONTRACT, written before the implementation (test-first per the
Rock-Solid Primitive playbook, speakbot:playbook-20251218-pb2o). It defines what
"correct" means; Phase 3 makes it green; Phase 4 hardens. Until the codec exists
the whole module skips (gated on `import skein.encoji` exposing the surface).

encoji encodes a SKEIN address — a full station name + a 50-bit folio short-hash
(the "identity") — into a fixed run of single-codepoint emoji, decodes it back,
and rejects non-canonical streams. It operates on an already-delimited address
stream; locating address boundaries inside surrounding text is the addressing
grammar's job (skein:brief-20260529-0yjl), aided by the first-codepoint sniffer.

============================================================================
The pinned alphabet (Phase 1, do NOT relitigate)
============================================================================
1024 single-codepoint emoji, Unicode age <= 13.0, default emoji presentation
(no variation selectors / ZWJ / modifiers). sha256 of the canonical
`index\\tU+XXXX\\n` table == db9eeaf666177d145e7e061af02b13f104e135b067a427b4c3f851a1ffec13da.
Index roles (the index->glyph map is arbitrary for correctness; ordering is by
(age, codepoint) so the cluster range renders on the oldest platforms):

    0-783      cluster    2-char clusters over [a-z . -]; index = c1*28 + c2  (28*28)
    784-811    singleton  one per cluster-alphabet char (forced boundary / final odd)
    812-821    digit      digit terminators 0-9
    822-838    control    brand + route-subset + type-subset (17 codepoints)
    839-1023   reserved   future expansion (185)

Cluster alphabet = "abcdefghijklmnopqrstuvwxyz.-" (28 chars, fixed order).
Extension sentinel = U+27BF DOUBLE CURLY LOOP, OUTSIDE the 1024 (never data).

============================================================================
Proposed API surface (the contract — open to the fell)
============================================================================
Layer boundary (fell-r1, codex): the PUBLIC surface speaks codepoints and
semantic labels (type words "alias"/"web"/"hash", route names); raw table
INDICES stay private to the codec. The Manifest carries version-specific config
as codepoints (not indices), so it is meaningful independent of the table order.

    skein.encoji.ALPHABET                 # the fixed, pinned alphabet (versioned data)
        .sha256        -> str             # == db9eeaf6...
        .size          -> int             # 1024
        .charset       -> str             # "abcdefghijklmnopqrstuvwxyz.-"
        .glyph(i)      -> str             # index -> single-codepoint glyph (0 <= i < 1024)
        .index(g)      -> int | None      # glyph -> index, or None if not a data glyph
        .role(i)       -> str             # "cluster"|"singleton"|"digit"|"control"|"reserved"
        .RANGES        -> tuple[(lo, hi, role_name), ...]   # the 5 index ranges
        .control_codepoints -> frozenset[int]   # the 17 control-range codepoints = the brand POOL

    skein.encoji.SENTINEL                 # str, the extension sentinel "\\u27bf"

    @dataclass(frozen=True)
    class ControlPartition:               # the per-version split of the control range, in CODEPOINTS
        brand: int                        # this version's brand codepoint
        types: Mapping[str, int]          # type word  -> codepoint  ({"alias":.., "web":.., "hash":..})
        routes: Mapping[str, int]         # route name -> codepoint  (provisional in v1)
        # invariant: every codepoint is one of ALPHABET.control_codepoints; and
        # {brand} / types.values() / routes.values() are mutually disjoint.

    @dataclass(frozen=True)
    class Manifest:                       # the per-version system of record (finding 9)
        version: int
        alphabet_sha256: str              # LOAD-BEARING: Codec() rejects a manifest whose digest
                                          #   != ALPHABET.sha256 (binds the manifest to its table).
        identity_length: int              # 5 for v1
        control_partition: ControlPartition
        normalization_profile: frozenset[int]   # codepoints normalize() strips (cosmetic/invisible)
        packing_order: str                # "big-endian"
        extension_sentinel: int           # 0x27BF
        prior_brand_registry: tuple[int, ...]    # control codepoints of prior versions' brands

    class Codec:
        def __init__(self, manifest: Manifest): ...   # ValueError if alphabet_sha256 != ALPHABET.sha256

        # --- station segment (string <-> emoji), sections A & B ---
        def encode_station(self, s: str) -> str
        def decode_station(self, glyphs: str) -> str

        # --- identity (50-bit <-> 5 glyphs), section D ---
        def encode_identity(self, value: int) -> str
        def decode_identity(self, glyphs: str) -> int

        # --- normalization (section B5 / E) ---
        def normalize(self, stream: str) -> str

        # --- full positional address (sections C & G) ---
        def encode(self, *, station: str, identity: int,
                   type: str, route: str | None = None) -> str
        def decode(self, stream: str) -> Decoded         # normalizes internally;
            # raises UnsupportedExtensionError when a (non-empty) extension is present.

        # --- free helper (finding 12) ---
        def station_badge(self, stream: str) -> str      # base minus the right-hand identity 5

    # version-agnostic O(1) sniffer (finding 12): glyph 0's codepoint in the brand POOL
    # (ALPHABET.control_codepoints). Module-level because it needs no version state.
    def looks_like_encoji(stream: str) -> bool

    @dataclass(frozen=True)
    class Decoded:
        station: str
        identity: int                     # the 50-bit value
        type: str                         # the type word ("alias" | "web" | "hash")
        route: str | None                 # the route name, or None

    # resolver bridge (section D2-D4). This is the encoji analogue of the rev3 addressing
    # resolver skein.address.resolve() (skein:finding-20260528-brgy): same station-scoped,
    # detect-and-error semantics (AmbiguousIdentity ~ AmbiguousShortHash, IdentityNotFound ~
    # ShortHashNotFound). Phase 3 MAY implement it as a thin adapter over that resolver,
    # specialized to the 50-bit identity rather than a hex prefix.
    class IdentityIndex(Protocol):
        def folios_with_identity(self, station: str, identity: int) -> list[str]: ...
            # returns matching FULL lowercase-hex digests (0, 1, or many)
    def resolve_identity(station: str, identity: int, index: IdentityIndex) -> str
        # 1 match -> that full digest; 0 -> IdentityNotFound; >1 -> AmbiguousIdentity

    Exceptions (all subclass EncojiError):
        EncojiError                       # base
        InvalidStationError               # encode: a char outside [a-z0-9.-]; unknown type/route label
        InvalidGlyphError                 # decode: a codepoint not in (1024 union {sentinel})
        NonCanonicalError                 # decode: re-encode != input (non-canonical clustering)
        StructuralError                   # full decode: layout / length / role / arity / empty station
        NoBrandError                      # glyph 0 in alphabet but not in the brand pool
        UnknownVersionError               # glyph 0 in the brand pool but not this version's brand
        UnsupportedExtensionError(.base: Decoded, .extension: str)   # a sentinel-delimited extension
        AmbiguousIdentity(.candidates: list[str])    # resolver: >1 colliding full digest
        IdentityNotFound                  # resolver: zero matches

============================================================================
Contract decisions (Phase 1 left these open; Phase 2 pins them as fixtures)
============================================================================
- Module home: `skein.encoji` (provisional — Phase 1 left knurl-vs-skein open).
  The contract imports one canonical name; relocating is a rename, not a redesign.
- The codec is parameterized by a Manifest (alphabet = fixed checksummed data;
  version-specific choices = manifest). `alphabet_sha256` is LOAD-BEARING — the
  Codec binds a manifest to exactly the pinned table and rejects a mismatch, so
  the manifest is not a decorative abstraction (fell-r1, codex). v1 ships exactly
  one alphabet; a future version with a different table is a new manifest bound
  to a new pinned alphabet. Tests build a Codec from a PROVISIONAL v1 manifest
  fixture so the positional tests run before Phase 3 pins the real partition;
  invariant tests constrain ANY manifest (incl. Phase 3's real one).
- Public API uses semantic labels (type words, route names), NOT table indices
  (fell-r1, codex): callers know "alias"/"web"/"hash", not 826. The provisional
  type-word -> codepoint and route-name -> codepoint maps are fixtured here;
  Phase 3 pins the real control-range glyph->meaning partition (the 🧶 brand,
  the route subset) within the structural invariants.
- normalization_profile: the tests enumerate a representative cosmetic/invisible
  strip set; any manifest's profile must be a SUPERSET of it (so Phase 3 cannot
  silently shrink normalization — fell-r1). Phase 3 MUST complete the
  Default_Ignorable set from UCD DerivedCoreProperties.txt (NOT among the three
  vendored inputs) and pin it — surfaced as the key open dependency for Phase 3.
- Extension: when a sentinel-delimited extension is present, decode() RAISES
  UnsupportedExtensionError carrying the resolved base + the raw extension
  (finding 13: "reject the extension with a specific error, NOT the whole
  address"). The base stays recoverable via the exception; this is a distinct,
  hard-to-ignore signal rather than an easily-missed flag (fell-r1, codex).
- G6 (a trailing sentinel with an empty extension payload): REJECT (StructuralError).
  Rationale: the bare base is the single canonical form; accepting `base + sentinel`
  with no payload would be a second encoding of the same address and break the
  re-encode-and-compare canonicality the decoder enforces everywhere else.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import unicodedata

import pytest

# --- import gate ------------------------------------------------------------
try:
    import skein.encoji as E
except Exception:  # ModuleNotFoundError today; broad so a partial stub still skips
    E = None

HAVE_ENCOJI = E is not None and all(
    hasattr(E, n) for n in ("ALPHABET", "Codec", "Manifest", "ControlPartition", "SENTINEL")
)
pytestmark = pytest.mark.skipif(
    not HAVE_ENCOJI,
    reason="encoji codec not implemented yet (Phase 3 makes this green)",
)

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover - hypothesis is a declared dev dep
    given = settings = st = None


# ===========================================================================
# Reference data — loaded from the PINNED Phase 1 artifact, not from the impl.
# Expectations are built from the documented index formulas + this table, so the
# tests are grounded in the spec and are NOT circular with the implementation.
# ===========================================================================
PINNED_SHA = "db9eeaf666177d145e7e061af02b13f104e135b067a427b4c3f851a1ffec13da"
CHARSET = "abcdefghijklmnopqrstuvwxyz.-"          # 28 chars, fixed order
SENTINEL_CP = 0x27BF                              # U+27BF DOUBLE CURLY LOOP
SENTINEL = chr(SENTINEL_CP)
IDENTITY_LEN = 5
MAX_IDENTITY = 1024 ** IDENTITY_LEN               # 2**50 exactly
CONTROL_LO, CONTROL_HI = 822, 838                 # permanent control boundary

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_ALPHA_JSON = _REPO_ROOT / "docs" / "emoji-codec" / "encoji-alphabet.json"


def _load_reference():
    """Load the pinned alphabet table and verify its checksum before use.

    The recompute mirrors the generator's canonical-table format exactly
    (encoji_alphabet.py:125 -> `f"{index}\\t{codepoint}\\n"` with
    codepoint == `f"U+{cp:04X}"`), so the import-time assert fails fast if the
    artifact ever drifts.
    """
    data = json.loads(_ALPHA_JSON.read_text(encoding="utf-8"))
    entries = data["alphabet"]
    assert len(entries) == 1024
    idx2cp = {}
    for e in entries:
        idx2cp[e["index"]] = int(e["codepoint"][2:], 16)
    assert sorted(idx2cp) == list(range(1024)), "index table has gaps"
    canon = "".join(f"{i}\tU+{idx2cp[i]:04X}\n" for i in range(1024)).encode()
    assert hashlib.sha256(canon).hexdigest() == PINNED_SHA, "pinned artifact drifted"
    return idx2cp


REF_CP = _load_reference()                         # index -> codepoint
REF_GLYPH = {i: chr(cp) for i, cp in REF_CP.items()}  # index -> single-codepoint str
REF_CP_SET = set(REF_CP.values())
CONTROL_CPS = frozenset(REF_CP[i] for i in range(CONTROL_LO, CONTROL_HI + 1))  # brand pool


# --- expectation builders (documented index formulas -> reference glyphs) ----
def gly(i: int) -> str:
    return REF_GLYPH[i]


def cl(a: str, b: str) -> str:
    """The cluster glyph for the ordered pair (a, b)."""
    return gly(CHARSET.index(a) * 28 + CHARSET.index(b))


def sg(c: str) -> str:
    """The forced-boundary / final-odd singleton glyph for char c."""
    return gly(784 + CHARSET.index(c))


def dg(d: str) -> str:
    """The digit-terminator glyph for digit d ('0'..'9')."""
    return gly(812 + int(d))


def idv(value: int) -> str:
    """Big-endian base-1024 identity: 5 glyphs, most-significant first."""
    return "".join(gly((value // (1024 ** k)) % 1024) for k in range(4, -1, -1))


# ===========================================================================
# The provisional v1 manifest fixture (see "Contract decisions" above).
# Control range 822-838: brand = index 822; type words alias/web/hash = 826/827/828;
# provisional route names = 823/824/825. Expressed as CODEPOINTS in the manifest.
# All disjoint, all within the control range. Phase 3 replaces the partition with
# the real v1 assignment (which must still satisfy the invariants tested below).
# ===========================================================================
PROV_BRAND_IDX = 822
PROV_TYPE_IDX = {"alias": 826, "web": 827, "hash": 828}
PROV_ROUTE_IDX = {"route_a": 823, "route_b": 824, "route_c": 825}  # names provisional (Phase 3)

# Representative cosmetic/invisible strip set (Phase 3 completes the DI set).
_VARIATION_SELECTORS = [0xFE0E, 0xFE0F]
_KEYCAP = [0x20E3]
_SKIN_TONE = list(range(0x1F3FB, 0x1F400))            # 1F3FB..1F3FF (Emoji_Component, cat Sk)
_DEFAULT_IGNORABLE = [
    0x200D, 0xFEFF, 0x2060, 0x034F, 0x00AD,           # ZWJ, BOM/ZWNBSP, WJ, CGJ, soft hyphen
    *range(0x200B, 0x2010),                           # zero-widths + LRM/RLM (200B..200F)
    *range(0x202A, 0x202F),                           # bidi embedding/override (202A..202E)
    *range(0x2066, 0x206A),                           # bidi isolates (2066..2069)
    *range(0xE0020, 0xE0080),                         # tag characters (E0020..E007F)
]
PROV_NORMALIZATION = frozenset(_VARIATION_SELECTORS + _KEYCAP + _SKIN_TONE + _DEFAULT_IGNORABLE)

TYPE_WORD = "alias"                                   # the type word used to build addresses
ROUTE_NAME = "route_a"                                # a route name used to build addresses


def brand_glyph() -> str:
    return gly(PROV_BRAND_IDX)


def type_glyph(word: str = TYPE_WORD) -> str:
    return gly(PROV_TYPE_IDX[word])


def route_glyph(name: str = ROUTE_NAME) -> str:
    return gly(PROV_ROUTE_IDX[name])


def _partition(**ov):
    return E.ControlPartition(
        brand=ov.get("brand", REF_CP[PROV_BRAND_IDX]),
        types=ov.get("types", {w: REF_CP[i] for w, i in PROV_TYPE_IDX.items()}),
        routes=ov.get("routes", {n: REF_CP[i] for n, i in PROV_ROUTE_IDX.items()}),
    )


def _manifest(**ov):
    return E.Manifest(
        version=ov.get("version", 1),
        alphabet_sha256=ov.get("alphabet_sha256", PINNED_SHA),
        identity_length=ov.get("identity_length", IDENTITY_LEN),
        control_partition=ov.get("control_partition", _partition()),
        normalization_profile=ov.get("normalization_profile", PROV_NORMALIZATION),
        packing_order=ov.get("packing_order", "big-endian"),
        extension_sentinel=ov.get("extension_sentinel", SENTINEL_CP),
        prior_brand_registry=ov.get("prior_brand_registry", ()),
    )


def _codec(**ov):
    """Build the provisional codec. Used by the @given tests (which receive
    Hypothesis args, not fixtures) and by the `codec` fixture."""
    return E.Codec(_manifest(**ov))


@pytest.fixture(scope="module")
def codec():
    return _codec()


# --- resolver fakes (mirror the rev3 addressing contract's style) -----------
class FakeIndex:
    """In-memory IdentityIndex for resolver tests. Deterministic, no I/O."""

    def __init__(self, mapping):
        # mapping: {(station, identity): [full_digest, ...]}
        self._m = {k: list(v) for k, v in mapping.items()}

    def folios_with_identity(self, station, identity):
        return list(self._m.get((station, identity), []))


FULL_A = "a" * 64
FULL_B = "b" * 64


# ===========================================================================
# Alphabet / manifest invariants — the pinned data and any valid manifest
# ===========================================================================
class TestAlphabetInvariants:
    def test_implementation_matches_pinned_checksum(self, codec):
        assert E.ALPHABET.sha256 == PINNED_SHA
        assert E.ALPHABET.size == 1024

    def test_implementation_reproduces_the_pinned_table(self, codec):
        # Binds the impl's ALPHABET to the checksummed artifact glyph-for-glyph;
        # the REF_*-based data invariants below ride on this binding.
        for i in range(1024):
            assert E.ALPHABET.glyph(i) == REF_GLYPH[i]

    def test_index_is_inverse_of_glyph(self, codec):
        for i in (0, 1, 27, 28, 783, 784, 811, 812, 821, 822, 838, 839, 1023):
            assert E.ALPHABET.index(E.ALPHABET.glyph(i)) == i

    def test_index_of_non_alphabet_is_none(self, codec):
        assert E.ALPHABET.index(SENTINEL) is None      # sentinel is outside the 1024
        assert E.ALPHABET.index("A") is None
        assert E.ALPHABET.index("́") is None      # combining acute

    def test_charset_is_pinned(self, codec):
        assert E.ALPHABET.charset == CHARSET

    def test_role_ranges_are_pinned(self, codec):
        spans = {
            "cluster": (0, 783), "singleton": (784, 811), "digit": (812, 821),
            "control": (822, 838), "reserved": (839, 1023),
        }
        for role, (lo, hi) in spans.items():
            assert E.ALPHABET.role(lo) == role
            assert E.ALPHABET.role(hi) == role

    def test_control_boundary_is_822_838(self, codec):
        # The boundary is permanent across versions (finding 10).
        assert E.ALPHABET.role(821) != "control"
        assert E.ALPHABET.role(822) == "control"
        assert E.ALPHABET.role(838) == "control"
        assert E.ALPHABET.role(839) == "reserved"

    def test_control_codepoints_are_the_brand_pool(self, codec):
        # The 17 control-range codepoints; version dispatch sniffs against these.
        assert E.ALPHABET.control_codepoints == CONTROL_CPS
        assert len(E.ALPHABET.control_codepoints) == 17

    def test_sentinel_constant(self, codec):
        assert E.SENTINEL == SENTINEL
        assert ord(E.SENTINEL) == SENTINEL_CP
        assert SENTINEL_CP not in REF_CP_SET           # never a data glyph

    def test_alphabet_is_normalization_closed(self, codec):
        # Standing DATA invariant (intentionally impl-independent; asserts over the
        # pinned table, which test_implementation_reproduces_the_pinned_table binds
        # to the impl). NFC == NFD == NFKC == self for every glyph (finding 8).
        for i in range(1024):
            g = REF_GLYPH[i]
            assert unicodedata.normalize("NFC", g) == g
            assert unicodedata.normalize("NFD", g) == g
            assert unicodedata.normalize("NFKC", g) == g

    def test_977_astral_glyphs(self, codec):
        # Standing DATA invariant. The UTF-16 surrogate landmine (finding 4):
        # 977 astral, 47 BMP.
        assert sum(1 for cp in REF_CP.values() if cp > 0xFFFF) == 977


def _assert_manifest_invariants(man):
    part = man.control_partition
    route_cps = set(part.routes.values())
    type_cps = set(part.types.values())
    brand = {part.brand}
    for cps in (brand, route_cps, type_cps):
        assert cps, "each control subset is non-empty"
        assert cps <= CONTROL_CPS, "control subsets live within the control codepoints"
    assert brand.isdisjoint(route_cps)
    assert brand.isdisjoint(type_cps)
    assert route_cps.isdisjoint(type_cps)
    assert man.identity_length == IDENTITY_LEN
    assert man.packing_order == "big-endian"
    assert man.extension_sentinel == SENTINEL_CP
    assert man.alphabet_sha256 == PINNED_SHA
    # normalization can only GROW, never shrink, relative to the pinned strip set.
    assert PROV_NORMALIZATION <= man.normalization_profile


class TestManifestInvariants:
    def test_provisional_manifest_satisfies_invariants(self, codec):
        _assert_manifest_invariants(codec.manifest)

    def test_alphabet_sha256_is_load_bearing(self):
        # A manifest naming a different alphabet must be rejected at construction
        # (the manifest is bound to exactly the pinned table, not decorative).
        with pytest.raises((ValueError, E.EncojiError)):
            _codec(alphabet_sha256="0" * 64)

    @pytest.mark.skipif(
        not (HAVE_ENCOJI and hasattr(E, "v1")),
        reason="Phase 3 pins the real v1 manifest (encoji.v1()); not present yet",
    )
    def test_real_v1_manifest_satisfies_invariants(self):
        # When Phase 3 ships the canonical v1 manifest, it must obey the same
        # structural invariants the provisional fixture does.
        _assert_manifest_invariants(E.v1().manifest)


# ===========================================================================
# A. Encoder canonical form (station -> emoji)
# ===========================================================================
class TestEncoderCanonicalForm:
    def test_A1_single_char_is_a_singleton(self, codec):
        assert codec.encode_station("a") == sg("a")

    def test_A2_even_all_letters_are_all_clusters(self, codec):
        assert codec.encode_station("abcd") == cl("a", "b") + cl("c", "d")

    def test_A3_odd_all_letters_end_in_a_singleton(self, codec):
        assert codec.encode_station("abc") == cl("a", "b") + sg("c")

    def test_A4_digit_terminates_current_cluster(self, codec):
        # 'abc3' -> 'ab', 'c' (forced-boundary singleton), '3'
        assert codec.encode_station("abc3") == cl("a", "b") + sg("c") + dg("3")

    def test_A5_leading_digit(self, codec):
        assert codec.encode_station("3dns") == dg("3") + cl("d", "n") + sg("s")

    def test_A6_consecutive_digits(self, codec):
        assert codec.encode_station("443") == dg("4") + dg("4") + dg("3")

    def test_A7_trailing_digit_flushes_leftover(self, codec):
        assert codec.encode_station("v2") == sg("v") + dg("2")

    def test_A8_dot_and_hyphen_are_cluster_chars(self, codec):
        # punycode 'xn--ab' -> 'xn', '--', 'ab'
        assert codec.encode_station("xn--ab") == cl("x", "n") + cl("-", "-") + cl("a", "b")

    def test_A8_leading_trailing_double_punctuation(self, codec):
        assert codec.encode_station("-a.") == cl("-", "a") + sg(".")
        assert codec.encode_station("..") == cl(".", ".")

    def test_A9_full_punycode_authority_round_trips(self, codec):
        s = "xn--80ak6aa92e.com"
        assert codec.decode_station(codec.encode_station(s)) == s

    def test_A10_non_canonical_authority_rejected(self, codec):
        # Uppercase / IDN / percent-encoded host are non-canonical input
        # (authority is validate-not-convert, per finding-20260528-brgy).
        for bad in ("Auth", "h%41st", "h ost", "café", "a::b", "A"):
            with pytest.raises(E.InvalidStationError):
                codec.encode_station(bad)

    def test_A10_empty_station_rejected_by_encoder(self, codec):
        with pytest.raises(E.InvalidStationError):
            codec.encode_station("")

    def test_A11_max_authority_length(self, codec):
        s = ("a" * 63 + ".") * 3 + "a" * 61          # 253 chars, label-shaped
        assert len(s) == 253
        out = codec.encode_station(s)
        assert codec.decode_station(out) == s


# ===========================================================================
# B. Decoder canonical-rejection (emoji -> station) — the core contract
# ===========================================================================
class TestDecoderCanonicalRejection:
    def test_B1_decode_by_range_then_round_trips(self, codec):
        assert codec.decode_station(cl("a", "b")) == "ab"
        assert codec.decode_station(cl("a", "b") + sg("c")) == "abc"
        assert codec.decode_station(dg("3") + cl("d", "n") + sg("s")) == "3dns"

    def test_B2_reject_two_adjacent_singletons_that_should_pair(self, codec):
        # sg(a)sg(b) decodes to 'ab' but canonical 'ab' is cl(a,b) -> reject.
        with pytest.raises(E.NonCanonicalError):
            codec.decode_station(sg("a") + sg("b"))

    def test_B3_reject_singleton_not_at_a_forced_boundary(self, codec):
        # sg(a)cl(b,c) decodes 'abc'; canonical is cl(a,b)sg(c) -> reject.
        with pytest.raises(E.NonCanonicalError):
            codec.decode_station(sg("a") + cl("b", "c"))

    def test_B3a_reject_differently_clustered_same_string(self, codec):
        # 'a.b': sg(a)cl(.,b) vs canonical cl(a,.)sg(b). The non-canonical
        # clustering decodes to the same string but must be rejected (knuth).
        assert codec.decode_station(cl("a", ".") + sg("b")) == "a.b"   # canonical OK
        with pytest.raises(E.NonCanonicalError):
            codec.decode_station(sg("a") + cl(".", "b"))

    def test_B4_reject_non_alphabet_codepoint(self, codec):
        with pytest.raises(E.InvalidGlyphError):
            codec.decode_station(cl("a", "b") + "Z")
        with pytest.raises(E.InvalidGlyphError):
            codec.decode_station(cl("a", "b") + "☃")   # snowman, not in alphabet

    def test_B5_normalize_strips_cosmetic_then_decodes(self, codec):
        # A variation selector / skin-tone stuck to a station glyph is stripped.
        dirty = cl("a", "b") + "️" + sg("c")
        assert codec.decode_station(codec.normalize(dirty)) == "abc"

    def test_B5_pipeline_order_modifier_does_not_shift_offsets(self, codec):
        # The decode is positional; normalize MUST run first or a stray modifier
        # miscounts the peel. decode() normalizes internally.
        clean = codec.encode(station="auth", identity=42, type=TYPE_WORD)
        # Insert a skin-tone modifier after the first identity glyph.
        cut = len(clean) - IDENTITY_LEN
        dirty = clean[: cut + 1] + "\U0001f3fb" + clean[cut + 1 :]
        assert codec.decode(dirty).station == "auth"
        assert codec.decode(dirty).identity == 42

    @pytest.mark.skipif(st is None, reason="hypothesis not installed")
    @settings(deadline=None, max_examples=300)
    @given(st.text(alphabet=CHARSET + "0123456789", min_size=1, max_size=40))
    def test_B6_round_trip_station(self, s):
        codec = _codec()
        assert codec.decode_station(codec.encode_station(s)) == s

    @pytest.mark.skipif(st is None, reason="hypothesis not installed")
    @settings(deadline=None, max_examples=300)
    @given(st.text(alphabet=CHARSET + "0123456789", min_size=1, max_size=40))
    def test_B6_canonical_stream_re_encodes_to_itself(self, s):
        codec = _codec()
        stream = codec.encode_station(s)
        assert codec.encode_station(codec.decode_station(stream)) == stream


# ===========================================================================
# C. Positional right-anchored decode (full stream) — peel, do NOT scan
# ===========================================================================
class TestPositionalDecode:
    def test_C1_layout_round_trips(self, codec):
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        d = codec.decode(stream)
        assert d.station == "auth"
        assert d.identity == 7
        assert d.type == TYPE_WORD
        assert d.route is None

    def test_C2_identity_is_the_last_five_even_when_glyphs_collide(self, codec):
        # Identity draws from the full 1024, so an identity glyph may equal the
        # brand/control codepoint. Right-anchoring still resolves it.
        value = PROV_BRAND_IDX * (1024 ** 4) + 5            # i4 == brand index
        stream = codec.encode(station="auth", identity=value, type=TYPE_WORD)
        assert stream.endswith(idv(value))
        assert codec.decode(stream).identity == value

    def test_C2a_type_subset_glyph_inside_identity_is_still_peeled(self, codec):
        # A right-to-left SCANNER (scan from the end for the first type-subset
        # control glyph) would mis-split here; positional peel takes the last 5
        # regardless. value places a type-subset index (826) at the last and the
        # middle identity glyph. Pins peel-not-scan deterministically (fell-r1).
        type_idx = PROV_TYPE_IDX[TYPE_WORD]                 # 826, a type-subset index
        for value in (type_idx, type_idx * (1024 ** 2)):
            stream = codec.encode(station="auth", identity=value, type=TYPE_WORD)
            assert codec.decode(stream).identity == value

    def test_C3_type_slot_must_be_a_type_subset_control_glyph(self, codec):
        base = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        cut = len(base) - IDENTITY_LEN
        # (a) a station glyph in the type slot -> reject
        bad_station = base[: cut - 1] + cl("z", "z") + base[cut:]
        with pytest.raises(E.StructuralError):
            codec.decode(bad_station)
        # (b) a control glyph of the WRONG sub-role (route code) in the type slot
        bad_role = base[: cut - 1] + route_glyph() + base[cut:]
        with pytest.raises(E.StructuralError):
            codec.decode(bad_role)

    def test_C4_station_run_must_be_indices_0_821(self, codec):
        # A reserved/control glyph mid-station is a parse error.
        bad = brand_glyph() + cl("a", "u") + gly(839) + cl("t", "h") + type_glyph() + idv(7)
        with pytest.raises(E.StructuralError):
            codec.decode(bad)

    def test_C5_route_present_is_detected(self, codec):
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD, route=ROUTE_NAME)
        d = codec.decode(stream)
        assert d.route == ROUTE_NAME
        assert d.station == "auth"
        assert d.identity == 7

    def test_C5_route_absent_is_detected(self, codec):
        d = codec.decode(codec.encode(station="auth", identity=7, type=TYPE_WORD))
        assert d.route is None

    def test_C5_reject_wrong_subrole_in_route_slot(self, codec):
        # A control glyph that is NOT a route (a type code, or the brand code) in
        # the route slot (right after brand) must reject — mirrors C3 for the type
        # slot. A decoder that treats ANY control glyph after brand as a route
        # would wrongly accept these (fell-r1, both reviewers).
        base = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        for wrong in (type_glyph(), brand_glyph()):
            with pytest.raises(E.StructuralError):
                codec.decode(base[:1] + wrong + base[1:])

    def test_C5a_partition_subsets_are_disjoint(self, codec):
        p = codec.manifest.control_partition
        brand, routes, types = {p.brand}, set(p.routes.values()), set(p.types.values())
        assert brand.isdisjoint(routes)
        assert brand.isdisjoint(types)
        assert routes.isdisjoint(types)

    def test_C6_glyph0_must_be_a_brand_pool_codepoint(self, codec):
        valid = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        # (a) glyph 0 is a normal alphabet glyph, not in the brand pool -> NoBrand
        no_brand = cl("a", "b") + valid[1:]
        with pytest.raises(E.NoBrandError):
            codec.decode(no_brand)
        # (b) glyph 0 is in the brand pool but is not this version's brand
        unknown = route_glyph() + valid[1:]             # control cp, not the v1 brand
        with pytest.raises(E.UnknownVersionError):
            codec.decode(unknown)

    def test_C_noncanonical_station_through_full_decode_rejected(self, codec):
        # The full decode() path must also enforce canonical-rejection — not only
        # decode_station() in isolation (fell-r1, adversarial). sg(a)sg(b) decodes
        # 'ab' but canonical is cl(a,b).
        stream = brand_glyph() + sg("a") + sg("b") + type_glyph() + idv(7)
        with pytest.raises(E.NonCanonicalError):
            codec.decode(stream)

    def test_C7_minimum_length_routeless_is_8(self, codec):
        # brand + station(>=1) + type + identity(5) == 8.
        too_short = brand_glyph() + type_glyph() + idv(7)   # 7 glyphs, no station
        with pytest.raises(E.StructuralError):
            codec.decode(too_short)

    def test_C7_minimum_length_route_present_is_9(self, codec):
        # brand + route + station(>=1) + type + identity(5) == 9.
        eight = brand_glyph() + route_glyph() + type_glyph() + idv(7)  # 8 glyphs, empty station
        with pytest.raises(E.StructuralError):
            codec.decode(eight)

    def test_C7a_empty_input_rejected(self, codec):
        with pytest.raises(E.StructuralError):
            codec.decode("")

    def test_C7b_empty_station_rejected(self, codec):
        # The 8-glyph [brand][route][type][identity x5] must NOT decode to an
        # empty station (fell r2): route present, but nothing between route and type.
        eight = brand_glyph() + route_glyph() + type_glyph() + idv(7)
        with pytest.raises(E.StructuralError):
            codec.decode(eight)

    def test_C8_identity_is_exactly_five(self, codec):
        base = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        # one identity glyph too few: the type slot now holds a real identity
        # glyph (not a type-subset control) -> structural failure, not mis-parse.
        with pytest.raises(E.StructuralError):
            codec.decode(base[:-1])
        # one identity glyph too many: the type slot moves onto an identity glyph.
        with pytest.raises(E.StructuralError):
            codec.decode(base + gly(0))


# ===========================================================================
# D. Identity / 50-bit / collision
# ===========================================================================
class TestIdentity:
    def test_D1_big_endian_packing_both_directions(self, codec):
        assert codec.encode_identity(0) == idv(0) == gly(0) * 5
        assert codec.encode_identity(MAX_IDENTITY - 1) == idv(MAX_IDENTITY - 1) == gly(1023) * 5
        # leftmost glyph is most-significant
        v = 1 * 1024 ** 4 + 2 * 1024 ** 3 + 3 * 1024 ** 2 + 4 * 1024 + 5
        assert codec.encode_identity(v) == gly(1) + gly(2) + gly(3) + gly(4) + gly(5)
        assert codec.decode_identity(codec.encode_identity(v)) == v

    def test_D1_out_of_range_value_rejected(self, codec):
        for bad in (-1, MAX_IDENTITY, MAX_IDENTITY + 7):
            with pytest.raises((E.StructuralError, ValueError)):
                codec.encode_identity(bad)

    def test_D1_decode_identity_wrong_length_rejected(self, codec):
        with pytest.raises(E.StructuralError):
            codec.decode_identity(idv(7)[:-1])          # 4 glyphs
        with pytest.raises(E.StructuralError):
            codec.decode_identity(idv(7) + gly(0))      # 6 glyphs

    @pytest.mark.skipif(st is None, reason="hypothesis not installed")
    @settings(deadline=None, max_examples=400)
    @given(st.integers(min_value=0, max_value=MAX_IDENTITY - 1))
    def test_D1_round_trip_no_modulo_bias(self, v):
        codec = _codec()
        assert codec.decode_identity(codec.encode_identity(v)) == v

    def test_D2_resolve_station_scoped_single_match(self, codec):
        idx = FakeIndex({("auth", 7): [FULL_A]})
        assert E.resolve_identity("auth", 7, idx) == FULL_A

    def test_D2_resolution_is_station_scoped(self, codec):
        # Same 50-bit identity, two stations -> the OTHER station's digest is not seen.
        idx = FakeIndex({("auth", 7): [FULL_A], ("web", 7): [FULL_B]})
        assert E.resolve_identity("auth", 7, idx) == FULL_A

    def test_D3_D4_collision_detects_and_errors_never_silently_picks(self, codec):
        idx = FakeIndex({("auth", 7): [FULL_A, FULL_B]})
        with pytest.raises(E.AmbiguousIdentity) as ei:
            E.resolve_identity("auth", 7, idx)
        assert set(ei.value.candidates) == {FULL_A, FULL_B}   # caller falls back to text full-hash

    def test_D_resolve_not_found(self, codec):
        with pytest.raises(E.IdentityNotFound):
            E.resolve_identity("auth", 7, FakeIndex({}))


# ===========================================================================
# E. Unicode / encoding gauntlet
# ===========================================================================
class TestUnicodeGauntlet:
    def test_E1_astral_heavy_address_round_trips(self, codec):
        # 977/1024 glyphs are astral; an all-high-index identity is all astral.
        value = MAX_IDENTITY - 1
        stream = codec.encode(station="auth", identity=value, type=TYPE_WORD)
        assert codec.decode(stream).identity == value
        # iterate-by-codepoint sanity: Python str length == glyph count
        assert len(stream) == 1 + 2 + 1 + 5             # brand+station(au,th)+type+id

    def test_E2_normalize_does_not_alter_a_clean_stream(self, codec):
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        assert codec.normalize(stream) == stream

    def test_E2_stray_combining_mark_rejected(self, codec):
        # A combining acute is NOT in the strip set and not in the alphabet.
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        with pytest.raises(E.InvalidGlyphError):
            codec.decode(stream[:2] + "́" + stream[2:])

    def test_E3_invisibles_stripped_before_between_and_at_edges(self, codec):
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        zwj, bom, bidi, tag = "‍", "﻿", "‮", "\U000e0061"
        dirty = bom + stream[:1] + zwj + stream[1:3] + bidi + stream[3:] + tag
        assert codec.normalize(dirty) == stream
        assert codec.decode(dirty).station == "auth"

    def test_E3_residual_non_alphabet_after_normalize_rejected(self, codec):
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        with pytest.raises(E.InvalidGlyphError):
            codec.decode(stream + "☃")             # snowman survives normalize

    def test_E4_lone_surrogate_rejected_cleanly(self, codec):
        # A lone/unpaired surrogate must reject, never crash or mis-decode. The
        # other E4 cases the finding names — overlong UTF-8, invalid continuation
        # bytes — are BYTES-level and cannot reach a `str` API: bytes.decode()
        # rejects them first. The lone surrogate is the only one representable as
        # a Python str, so it is the str-API's E4 coverage. (If Phase 3 adds a
        # bytes entry point, add bytes-rejection tests there.)
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        with pytest.raises(E.EncojiError):
            codec.decode(stream + "\ud800")

    def test_E5_byte_round_trip_across_encodings(self, codec):
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        for enc in ("utf-8", "utf-16", "utf-32"):
            assert stream.encode(enc).decode(enc) == stream
        assert codec.decode(stream.encode("utf-8").decode("utf-8")).station == "auth"

    def test_E6_bidi_control_stripped_order_preserved(self, codec):
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        rtl = "‮"                                   # RIGHT-TO-LEFT OVERRIDE
        normalized = codec.normalize(stream[:1] + rtl + stream[1:])
        assert normalized == stream                      # codepoint order unchanged


# ===========================================================================
# F. Rendering / width fallback (display-only; decode rides on codepoints)
# ===========================================================================
class TestRenderingFallback:
    def test_F1_unrenderable_glyph_still_decodes(self, codec):
        # An address built from age-13 (high-index) glyphs would tofu on a
        # pre-2020 device, but decode is by codepoint, not pixels.
        value = MAX_IDENTITY - 1                         # all index-1023 glyphs
        stream = codec.encode(station="auth", identity=value, type=TYPE_WORD)
        assert codec.decode(stream).identity == value

    def test_F2_same_codepoints_decode_identically(self, codec):
        # Font substitution changes pixels, never codepoints.
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        assert codec.decode(stream).station == codec.decode("".join(stream)).station

    def test_F3_terminal_width_is_display_only(self, codec):
        # No codec behavior depends on rendered width; documented as a no-op so
        # the gauntlet's section F is explicitly accounted for.
        pytest.skip("terminal single/double-width is display-only; no codec behavior")


# ===========================================================================
# G. Extension sentinel (finding 13)
# ===========================================================================
class TestExtensionSentinel:
    def test_G1_sentinel_absent_decodes_normally(self, codec):
        d = codec.decode(codec.encode(station="auth", identity=7, type=TYPE_WORD))
        assert d.station == "auth" and d.identity == 7

    def test_G2_split_at_first_sentinel_base_resolves(self, codec):
        base = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        payload = cl("x", "y") + sg("z")
        with pytest.raises(E.UnsupportedExtensionError) as ei:
            codec.decode(base + SENTINEL + payload)
        assert ei.value.base.station == "auth"           # id = 5 glyphs just before sentinel
        assert ei.value.base.identity == 7
        assert ei.value.extension == payload

    def test_G2_split_is_at_the_FIRST_sentinel(self, codec):
        base = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        payload = cl("x", "y") + SENTINEL + sg("z")      # a second sentinel lives in the payload
        with pytest.raises(E.UnsupportedExtensionError) as ei:
            codec.decode(base + SENTINEL + payload)
        assert ei.value.base.station == "auth"
        assert ei.value.extension == payload

    def test_G3_base_resolved_equals_the_bare_base(self, codec):
        # finding 11/13: the base stays resolvable when the extension is
        # unsupported, AND it must not be silently equal to the bare base — here
        # the bare base DECODES while the extended form RAISES (distinct outcomes),
        # and the recovered base equals the bare decode.
        base = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        bare = codec.decode(base)
        with pytest.raises(E.UnsupportedExtensionError) as ei:
            codec.decode(base + SENTINEL + cl("x", "y"))
        assert ei.value.base == bare

    def test_G4_sentinel_never_a_data_glyph(self, codec):
        assert E.ALPHABET.index(SENTINEL) is None
        assert SENTINEL_CP not in REF_CP_SET
        # The valid codepoint set is the 1024 union {sentinel}: normalize accepts it.
        assert codec.normalize(SENTINEL) == SENTINEL

    def test_G5_normalize_does_not_strip_the_sentinel(self, codec):
        base = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        stream = base + SENTINEL + cl("x", "y")
        assert SENTINEL in codec.normalize(stream)

    def test_G6_trailing_sentinel_empty_extension_rejected(self, codec):
        # Contract decision: empty extension payload is non-canonical (the bare
        # base is the single canonical form). REJECT — and NOT as an
        # UnsupportedExtension (there is no extension), but as malformed framing.
        base = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        with pytest.raises(E.StructuralError):
            codec.decode(base + SENTINEL)

    def test_G7_leading_sentinel_empty_base_rejected(self, codec):
        # glyph 0 == sentinel: empty base / not in the brand pool.
        with pytest.raises((E.NoBrandError, E.StructuralError)):
            codec.decode(SENTINEL + cl("x", "y"))
        assert E.looks_like_encoji(SENTINEL + cl("x", "y")) is False


# ===========================================================================
# Free helpers (finding 12): sniffer, station badge
# ===========================================================================
class TestHelpers:
    def test_looks_like_encoji_true_for_valid_address(self, codec):
        assert E.looks_like_encoji(codec.encode(station="auth", identity=7, type=TYPE_WORD)) is True

    def test_looks_like_encoji_true_for_unknown_version(self, codec):
        # glyph 0 in the brand pool (any control codepoint) -> looks like encoji.
        assert E.looks_like_encoji(route_glyph() + cl("a", "b")) is True

    def test_looks_like_encoji_false_for_non_brand_leading_glyph(self, codec):
        assert E.looks_like_encoji(cl("a", "b") + cl("c", "d")) is False

    def test_looks_like_encoji_false_for_non_alphabet(self, codec):
        assert E.looks_like_encoji("hello") is False

    def test_looks_like_encoji_false_for_empty(self, codec):
        assert E.looks_like_encoji("") is False

    def test_station_badge_is_base_minus_identity(self, codec):
        stream = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        assert codec.station_badge(stream) == stream[:-IDENTITY_LEN]

    def test_station_badge_operates_on_the_base_before_a_sentinel(self, codec):
        base = codec.encode(station="auth", identity=7, type=TYPE_WORD)
        assert codec.station_badge(base + SENTINEL + cl("x", "y")) == base[:-IDENTITY_LEN]


# ===========================================================================
# Full-stream property: encode/decode is a faithful round-trip
# ===========================================================================
@pytest.mark.skipif(st is None, reason="hypothesis not installed")
class TestFullRoundTrip:
    @settings(deadline=None, max_examples=300)
    @given(
        station=st.text(alphabet=CHARSET + "0123456789", min_size=1, max_size=30),
        identity=st.integers(min_value=0, max_value=MAX_IDENTITY - 1),
        route=st.sampled_from((None, "route_a", "route_b", "route_c")),
    )
    def test_full_round_trip(self, station, identity, route):
        codec = _codec()
        stream = codec.encode(station=station, identity=identity, type=TYPE_WORD, route=route)
        d = codec.decode(stream)
        assert d.station == station
        assert d.identity == identity
        assert d.route == route
        assert d.type == TYPE_WORD

    @settings(deadline=None, max_examples=200)
    @given(
        station=st.text(alphabet=CHARSET + "0123456789", min_size=1, max_size=30),
        identity=st.integers(min_value=0, max_value=MAX_IDENTITY - 1),
    )
    def test_canonical_stream_re_encodes_to_itself(self, station, identity):
        codec = _codec()
        stream = codec.encode(station=station, identity=identity, type=TYPE_WORD)
        d = codec.decode(stream)
        assert codec.encode(
            station=d.station, identity=d.identity, type=d.type, route=d.route,
        ) == stream
