"""encoji — the reversible emoji codec (Phase 3 implementation).

encoji encodes a SKEIN address (a full station name + a 50-bit folio short-hash,
the "identity") into a fixed run of single-codepoint emoji, decodes it back, and
rejects non-canonical streams. The design, the curated 1024-glyph alphabet, the
decode pipeline, and the edge-case gauntlet are pinned in skein:finding-20260529-uyt5
(Phase 1, rev 6); the executable contract is tests/test_encoji.py (Phase 2,
fell-clean). This module implements that contract.

The 1024-glyph alphabet is FIXED, checksummed data (loaded from
docs/emoji-codec/encoji-alphabet.json, sha256 db9eeaf6...). Everything
version-specific lives in a Manifest (finding 9: "alphabet as versioned data,
manifest as system of record"). v1() returns the canonical v1 codec.

Iterate-by-codepoint discipline (finding 4): 977/1024 glyphs are astral, so
indexing by UTF-16 code unit would split glyphs. Python 3 `str` iteration is by
codepoint, which this module relies on throughout; a bytes entry point would have
to decode first.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol

# ---------------------------------------------------------------------------
# Paths to the pinned, vendored data (offline-reproducible).
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_ALPHA_JSON = _REPO_ROOT / "docs" / "emoji-codec" / "encoji-alphabet.json"
_DERIVED_CORE_PROPS = _REPO_ROOT / "docs" / "emoji-codec" / "inputs" / "DerivedCoreProperties.txt"

CHARSET = "abcdefghijklmnopqrstuvwxyz.-"          # 28 chars, fixed order (finding 1)
SENTINEL_CP = 0x27BF                              # U+27BF DOUBLE CURLY LOOP (finding 13)
SENTINEL = chr(SENTINEL_CP)
IDENTITY_LENGTH = 5                               # v1 (finding 7): 1024**5 == 2**50
MAX_IDENTITY = 1024 ** IDENTITY_LENGTH
PINNED_SHA = "db9eeaf666177d145e7e061af02b13f104e135b067a427b4c3f851a1ffec13da"

_CONTROL_LO, _CONTROL_HI = 822, 838               # permanent control boundary (finding 10)

# Index-range roles (the index->glyph map is arbitrary for correctness; the
# ordering by (age, codepoint) keeps the cluster range on the oldest platforms).
_RANGES = (
    (0, 783, "cluster"),
    (784, 811, "singleton"),
    (812, 821, "digit"),
    (822, 838, "control"),
    (839, 1023, "reserved"),
)


# ===========================================================================
# Exception hierarchy
# ===========================================================================
class EncojiError(Exception):
    """Base for every encoji error."""


class InvalidStationError(EncojiError):
    """Encode: a char outside [a-z0-9.-], an empty station, or an unknown
    type/route label."""


class InvalidGlyphError(EncojiError):
    """Decode: a codepoint not in (the 1024 data glyphs union {sentinel})."""


class NonCanonicalError(EncojiError):
    """Decode: re-encoding the decoded string does not reproduce the input
    (non-canonical clustering)."""


class StructuralError(EncojiError):
    """Full decode: layout / length / role / arity / empty-station / framing."""


class NoBrandError(EncojiError):
    """Glyph 0 is a data glyph but not in the brand pool (no brand)."""


class UnknownVersionError(EncojiError):
    """Glyph 0 is in the brand pool but is not this version's brand."""


class UnsupportedExtensionError(EncojiError):
    """A sentinel-delimited extension is present. The base is resolved and
    carried on the exception; only the extension is unsupported (finding 13)."""

    def __init__(self, base: "Decoded", extension: str):
        self.base = base
        self.extension = extension
        super().__init__(f"unsupported encoji extension ({len(extension)} glyph(s))")


class AmbiguousIdentity(EncojiError):
    """Resolver: more than one full digest collides on the 50-bit identity."""

    def __init__(self, candidates):
        self.candidates = list(candidates)
        super().__init__(f"ambiguous identity: {len(self.candidates)} colliding digests")


class IdentityNotFound(EncojiError):
    """Resolver: zero matches for the identity in the station scope."""


# ===========================================================================
# ALPHABET — the fixed, checksummed data table
# ===========================================================================
class _Alphabet:
    """The pinned 1024-glyph alphabet. Loaded from the checksummed Phase 1
    artifact and self-verified at import (the sha256 must reproduce the pinned
    digest, so a drifted artifact fails fast)."""

    def __init__(self, json_path: pathlib.Path):
        data = json.loads(json_path.read_text(encoding="utf-8"))
        entries = data["alphabet"]
        idx2cp = {}
        for e in entries:
            idx2cp[int(e["index"])] = int(e["codepoint"][2:], 16)
        if sorted(idx2cp) != list(range(1024)):
            raise ValueError("encoji alphabet index table has gaps")
        # Recompute the canonical digest exactly as the generator emits it.
        canon = "".join(f"{i}\tU+{idx2cp[i]:04X}\n" for i in range(1024)).encode()
        digest = hashlib.sha256(canon).hexdigest()
        if digest != PINNED_SHA:
            raise ValueError(f"encoji alphabet drifted: {digest} != {PINNED_SHA}")

        self._sha256 = digest
        self._idx2cp = idx2cp
        self._idx2glyph = {i: chr(cp) for i, cp in idx2cp.items()}
        self._glyph2idx = {g: i for i, g in self._idx2glyph.items()}
        self.RANGES = _RANGES
        self.control_codepoints = frozenset(
            idx2cp[i] for i in range(_CONTROL_LO, _CONTROL_HI + 1)
        )

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def size(self) -> int:
        return 1024

    @property
    def charset(self) -> str:
        return CHARSET

    def glyph(self, i: int) -> str:
        """index -> single-codepoint glyph (0 <= i < 1024)."""
        return self._idx2glyph[i]

    def index(self, g: str) -> Optional[int]:
        """glyph -> index, or None if g is not a data glyph (e.g. the sentinel,
        an ASCII char, a combining mark, a lone surrogate)."""
        return self._glyph2idx.get(g)

    def role(self, i: int) -> str:
        """index -> "cluster"|"singleton"|"digit"|"control"|"reserved"."""
        for lo, hi, name in _RANGES:
            if lo <= i <= hi:
                return name
        raise IndexError(f"index out of range: {i}")


ALPHABET = _Alphabet(_ALPHA_JSON)


# ===========================================================================
# Default_Ignorable normalization profile (vendored UCD 15.0.0)
# ===========================================================================
def _load_default_ignorable(path: pathlib.Path) -> frozenset:
    """Enumerate the exact Default_Ignorable_Code_Point set from the vendored
    DerivedCoreProperties.txt (finding 3: don't ship "all DI" unpinned)."""
    cps = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(";")]
            if len(parts) < 2 or parts[1] != "Default_Ignorable_Code_Point":
                continue
            rng = parts[0]
            if ".." in rng:
                lo, hi = rng.split("..")
                cps.update(range(int(lo, 16), int(hi, 16) + 1))
            else:
                cps.add(int(rng, 16))
    return frozenset(cps)


# Variation selectors are themselves Default_Ignorable but are pinned explicitly
# for robustness; the keycap (Me) and skin-tone modifiers (Sk) are genuinely NOT
# Default_Ignorable, so a naive "strip all DI" would miss them (finding 3).
_VARIATION_SELECTORS = frozenset((0xFE0E, 0xFE0F))
_KEYCAP = frozenset((0x20E3,))
_SKIN_TONE = frozenset(range(0x1F3FB, 0x1F400))   # 1F3FB..1F3FF (Emoji_Component)

V1_NORMALIZATION_PROFILE = frozenset(
    _load_default_ignorable(_DERIVED_CORE_PROPS)
    | _VARIATION_SELECTORS
    | _KEYCAP
    | _SKIN_TONE
)


# ===========================================================================
# Manifest / ControlPartition
# ===========================================================================
@dataclass(frozen=True)
class ControlPartition:
    """The per-version split of the control range, expressed in CODEPOINTS so it
    is meaningful independent of the table order."""

    brand: int
    types: Mapping[str, int]
    routes: Mapping[str, int]


@dataclass(frozen=True)
class Manifest:
    """The per-version system of record (finding 9)."""

    version: int
    alphabet_sha256: str
    identity_length: int
    control_partition: ControlPartition
    normalization_profile: frozenset
    packing_order: str
    extension_sentinel: int
    prior_brand_registry: tuple = ()


# ===========================================================================
# Decoded result
# ===========================================================================
@dataclass(frozen=True)
class Decoded:
    station: str
    identity: int
    type: str
    route: Optional[str]


# ===========================================================================
# Codec
# ===========================================================================
class Codec:
    def __init__(self, manifest: Manifest):
        if manifest.alphabet_sha256 != ALPHABET.sha256:
            raise ValueError(
                "manifest.alphabet_sha256 does not match the pinned alphabet "
                f"({manifest.alphabet_sha256} != {ALPHABET.sha256})"
            )
        self.manifest = manifest
        part = manifest.control_partition
        self._brand_cp = part.brand
        self._type_by_cp = {cp: w for w, cp in part.types.items()}
        self._route_by_cp = {cp: n for n, cp in part.routes.items()}
        self._type_cps = frozenset(part.types.values())
        self._route_cps = frozenset(part.routes.values())
        self._sentinel = chr(manifest.extension_sentinel)
        self._profile = manifest.normalization_profile
        self._identity_length = manifest.identity_length

    # --- station segment (string <-> emoji), sections A & B ----------------
    def encode_station(self, s: str) -> str:
        if not s:
            raise InvalidStationError("empty station")
        for c in s:
            if not (c in CHARSET or c in "0123456789"):
                raise InvalidStationError(f"char not in [a-z0-9.-]: {c!r}")
        out = []
        leftover = None                     # a pending cluster-alphabet char
        for c in s:
            if c in "0123456789":
                if leftover is not None:    # flush the odd leftover at the boundary
                    out.append(ALPHABET.glyph(784 + CHARSET.index(leftover)))
                    leftover = None
                out.append(ALPHABET.glyph(812 + int(c)))
            else:                           # a cluster-alphabet char
                if leftover is None:
                    leftover = c
                else:
                    out.append(ALPHABET.glyph(CHARSET.index(leftover) * 28 + CHARSET.index(c)))
                    leftover = None
        if leftover is not None:
            out.append(ALPHABET.glyph(784 + CHARSET.index(leftover)))
        return "".join(out)

    def decode_station(self, glyphs: str) -> str:
        if not glyphs:
            return ""
        chars = []
        for ch in glyphs:                   # iterate by codepoint
            idx = ALPHABET.index(ch)
            if idx is None:
                raise InvalidGlyphError(f"not a data glyph: {ch!r}")
            role = ALPHABET.role(idx)
            if role == "cluster":
                chars.append(CHARSET[idx // 28])
                chars.append(CHARSET[idx % 28])
            elif role == "singleton":
                chars.append(CHARSET[idx - 784])
            elif role == "digit":
                chars.append(str(idx - 812))
            else:                           # control / reserved is never a station glyph
                raise StructuralError(f"non-station glyph in station run: index {idx}")
        s = "".join(chars)
        # Canonical-rejection: re-encode and compare (knuth: sound AND complete).
        if self.encode_station(s) != glyphs:
            raise NonCanonicalError("station is not in canonical clustering")
        return s

    # --- identity (50-bit <-> 5 glyphs), section D -------------------------
    def encode_identity(self, value: int) -> str:
        if not (0 <= value < 1024 ** self._identity_length):
            raise ValueError(f"identity out of range [0, 1024**{self._identity_length}): {value}")
        n = self._identity_length
        return "".join(
            ALPHABET.glyph((value // (1024 ** k)) % 1024) for k in range(n - 1, -1, -1)
        )

    def decode_identity(self, glyphs: str) -> int:
        cps = list(glyphs)                  # by codepoint
        if len(cps) != self._identity_length:
            raise StructuralError(
                f"identity must be exactly {self._identity_length} glyphs, got {len(cps)}"
            )
        value = 0
        for ch in cps:
            idx = ALPHABET.index(ch)
            if idx is None:
                raise InvalidGlyphError(f"not a data glyph in identity: {ch!r}")
            value = value * 1024 + idx      # big-endian: first glyph is most-significant
        return value

    # --- normalization (section B5 / E) ------------------------------------
    def normalize(self, stream: str) -> str:
        profile = self._profile
        # Iterate by codepoint; the sentinel is never in the profile (invariant),
        # so framing is preserved. A lone surrogate is not in the profile either,
        # so it survives to be rejected later as a non-alphabet glyph (never crash).
        return "".join(ch for ch in stream if ord(ch) not in profile)

    # --- full positional address (sections C & G) --------------------------
    def encode(self, *, station: str, identity: int,
               type: str, route: Optional[str] = None) -> str:
        if type not in self.manifest.control_partition.types:
            raise InvalidStationError(f"unknown type label: {type!r}")
        parts = [chr(self._brand_cp)]
        if route is not None:
            if route not in self.manifest.control_partition.routes:
                raise InvalidStationError(f"unknown route label: {route!r}")
            parts.append(chr(self.manifest.control_partition.routes[route]))
        parts.append(self.encode_station(station))
        parts.append(chr(self.manifest.control_partition.types[type]))
        parts.append(self.encode_identity(identity))
        return "".join(parts)

    def decode(self, stream: str) -> Decoded:
        stream = self.normalize(stream)
        if self._sentinel in stream:
            p = stream.index(self._sentinel)
            base_part = stream[:p]
            extension = stream[p + 1:]
            base = self._parse_base(base_part)      # base stays resolvable (finding 13)
            if extension == "":
                # Empty extension payload is non-canonical framing, not an
                # "unsupported extension" (there is no extension). REJECT.
                raise StructuralError("trailing sentinel with empty extension payload")
            raise UnsupportedExtensionError(base, extension)
        return self._parse_base(stream)

    def _parse_base(self, s: str) -> Decoded:
        glyphs = list(s)                    # by codepoint; s has no sentinel
        # 1. Every glyph in the base must be a data glyph (the sentinel was split
        #    out; anything not in the 1024 is invalid). Glyph-validity is checked
        #    before structure so a stray non-alphabet codepoint surfaces as an
        #    InvalidGlyphError rather than a length/role mis-parse.
        for ch in glyphs:
            if ALPHABET.index(ch) is None:
                raise InvalidGlyphError(f"not a data glyph: {ch!r}")
        n = len(glyphs)
        if n == 0:
            raise StructuralError("empty stream")
        # 2. Brand dispatch on glyph 0.
        cp0 = ord(glyphs[0])
        if cp0 not in ALPHABET.control_codepoints:
            raise NoBrandError("glyph 0 is not in the brand pool")
        if cp0 != self._brand_cp:
            raise UnknownVersionError("glyph 0 is a brand-pool codepoint but not this version's brand")
        # 3. Route detection at position 1 (immediately after brand).
        route = None
        station_start = 1
        if n >= 2 and ord(glyphs[1]) in ALPHABET.control_codepoints:
            cp1 = ord(glyphs[1])
            if cp1 in self._route_cps:
                route = self._route_by_cp[cp1]
                station_start = 2
            else:
                # A control glyph that is not a route (a type code or the brand
                # code) in the route slot is a wrong-sub-role error.
                raise StructuralError("non-route control glyph in the route slot")
        # 4. Positional layout: identity = last 5, type = position -6,
        #    station = the run between station_start and the type slot (>= 1 glyph).
        idn = self._identity_length
        if n - station_start < idn + 2:     # need >=1 station + type(1) + identity(idn)
            raise StructuralError("too short (empty station or missing slots)")
        type_pos = n - idn - 1
        type_cp = ord(glyphs[type_pos])
        if type_cp not in self._type_cps:
            raise StructuralError("type slot is not a type-subset control glyph")
        type_word = self._type_by_cp[type_cp]
        # 5. Station run must be indices 0-821; anything >= 822 (control or
        #    reserved) mid-station is a parse error.
        station_glyphs = glyphs[station_start:type_pos]
        for ch in station_glyphs:
            if ALPHABET.index(ch) >= _CONTROL_LO:
                raise StructuralError("control/reserved glyph in the station run")
        station = self.decode_station("".join(station_glyphs))   # canonical-rejection inside
        # 6. Identity = the last idn glyphs.
        identity = self.decode_identity("".join(glyphs[n - idn:]))
        return Decoded(station=station, identity=identity, type=type_word, route=route)

    # --- free helper (finding 12) ------------------------------------------
    def station_badge(self, stream: str) -> str:
        """The base minus the right-hand identity glyphs — a per-station
        fingerprint. Operates on the base before the first sentinel and does NOT
        validate the extension (decode() is the validator)."""
        if self._sentinel in stream:
            stream = stream[:stream.index(self._sentinel)]
        return stream[:-self._identity_length]


# ===========================================================================
# Module-level sniffer (finding 12): version-agnostic, O(1), no codec state.
# ===========================================================================
def looks_like_encoji(stream: str) -> bool:
    """True iff glyph 0's codepoint is in the brand pool
    (ALPHABET.control_codepoints)."""
    if not stream:
        return False
    return ord(stream[0]) in ALPHABET.control_codepoints


# ===========================================================================
# Resolver bridge (sections D2-D4): station-scoped, detect-and-error.
# ===========================================================================
class IdentityIndex(Protocol):
    def folios_with_identity(self, station: str, identity: int) -> list: ...


def resolve_identity(station: str, identity: int, index: IdentityIndex) -> str:
    """1 match -> that full digest; 0 -> IdentityNotFound; >1 -> AmbiguousIdentity."""
    matches = list(index.folios_with_identity(station, identity))
    if not matches:
        raise IdentityNotFound(f"no folio with identity {identity} in station {station!r}")
    if len(matches) > 1:
        raise AmbiguousIdentity(matches)
    return matches[0]


# ===========================================================================
# The canonical v1 codec
# ===========================================================================
# Real v1 control partition (indices 822-838 -> codepoints). The intended 🧶 BALL
# OF YARN brand (U+1F9F6) lands at index 900 — in the RESERVED range, NOT the
# control range — so it cannot be the v1 brand without a table revision (which
# would be a new version, finding 7/9). The partition is assigned sequentially
# from the lowest control codepoints, drawing the brand and the three pinned type
# words from the visually-distinct animal/face block (822-826) so the load-bearing
# glyphs resist human-relay confusion (finding 11). Route names are provisional,
# opaque, v1-renamable identifiers (the contract pins only their structural slot +
# disjointness); v1 route SEMANTICS are deferred.
_CONTROL_CP = {i: ord(ALPHABET.glyph(i)) for i in range(_CONTROL_LO, _CONTROL_HI + 1)}

V1_CONTROL_PARTITION = ControlPartition(
    brand=_CONTROL_CP[822],                         # 🦓 ZEBRA FACE (U+1F993) — the version magic glyph
    types={
        "alias": _CONTROL_CP[823],                  # 🦔 HEDGEHOG (U+1F994)
        "web": _CONTROL_CP[824],                    # 🦕 SAUROPOD (U+1F995)
        "hash": _CONTROL_CP[825],                   # 🦖 T-REX (U+1F996)
    },
    routes={
        "route_a": _CONTROL_CP[826],                # 🦗 CRICKET (U+1F997)
        "route_b": _CONTROL_CP[827],                # 🧐 FACE WITH MONOCLE (U+1F9D0)
        "route_c": _CONTROL_CP[828],                # 🧑 ADULT (U+1F9D1)
    },
)

V1_MANIFEST = Manifest(
    version=1,
    alphabet_sha256=PINNED_SHA,
    identity_length=IDENTITY_LENGTH,
    control_partition=V1_CONTROL_PARTITION,
    normalization_profile=V1_NORMALIZATION_PROFILE,
    packing_order="big-endian",
    extension_sentinel=SENTINEL_CP,
    prior_brand_registry=(),                        # v1 has no priors
)


def v1() -> Codec:
    """The canonical v1 codec (real control partition + real normalization profile)."""
    return Codec(V1_MANIFEST)
