#!/usr/bin/env python3
"""Deterministic generator for the encoji codec 1024-glyph alphabet (Phase 1).

encoji = encode + emoji: a reversible encoding of a SKEIN address (full station +
50-bit folio short-hash) into a fixed run of single-codepoint emoji.

PINNED INPUTS (Unicode 15.0.0 UCD; DerivedAge is monotonic, so a codepoint's age
value never changes across UCD versions -> using 15.0.0 to compute age<=13.0 is
stable and reproducible):
  emoji-data.txt  https://www.unicode.org/Public/15.0.0/ucd/emoji/emoji-data.txt
  DerivedAge.txt  https://www.unicode.org/Public/15.0.0/ucd/DerivedAge.txt
  emoji-test.txt  https://www.unicode.org/Public/emoji/15.0/emoji-test.txt   (group/subgroup only)

CANDIDATE POOL (1113):
  single codepoint; Emoji_Presentation=Yes (renders as emoji with NO VS-16);
  NOT Emoji_Modifier, NOT Emoji_Component; Unicode age <= 13.0 (Emoji 13.0 / 2020).

  Why age<=13.0: the pinned spec (finding-20260528-brgy) asked for age<=9.0, but that
  pool is only 879 < 1024 -- 1024 clean single-codepoint emoji do not exist at 9.0.
  Decision (pair, 2026-05-29): raise to 13.0 (2020), pool 1113, curate down to 1024.

CURATION TRIM (1113 -> 1024, exactly 89 removed). Tone: cleaned up, not sanitized.
  distinctness   clock faces U+1F550..U+1F567 (24); geometric size/UI near-dups (4)
  text-boundary  script-letterform boxes (29); character-adjacent punctuation marks (13)
  off-putting    prohibition signs (9); gross/weapon/offensive (8); gross relief-valve (2)

ASSIGNMENT: surviving 1024 sorted by (Unicode age, codepoint) ascending -> index 0..1023.
  The index->glyph map is arbitrary for CORRECTNESS (round-trip rides only on codepoint
  identity), so the ordering is free to choose. Age-then-codepoint is deterministic and
  reproducible, and it concentrates the oldest / most-universally-supported glyphs at the
  lowest indices. Consequence (verified): the CLUSTER range 0-783 -- the bulk of station
  encoding and the dominant visual mass of a station badge -- is entirely Unicode age<=9.0,
  so it renders on any Unicode 9.0+ platform. Singletons/digits/control and the identity
  field (which draws from the full 1024 for the clean 2^50 packing) may include up to
  age-13 glyphs; those degrade to tofu on pre-2020 devices but still round-trip (decode is
  by codepoint, not pixels). Note: the final 1024 holds 796 age<=9.0 glyphs (the 89 drops
  removed many old glyphs -- clocks/prohibition/script-boxes are age 6.0), so 0-783 all
  age<=9.0 is feasible (783 < 796) but the full active+control range 0-838 is NOT.
"""
import json, hashlib, unicodedata, sys

def load(path):
    d = {}
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if not line: continue
        parts = [p.strip() for p in line.split(";")]
        rng, val = parts[0], (parts[1] if len(parts) > 1 else None)
        if ".." in rng: a, b = rng.split(".."); lo, hi = int(a, 16), int(b, 16)
        else: lo = hi = int(rng, 16)
        for cp in range(lo, hi + 1): d.setdefault(cp, []).append(val)
    return d

def main(indir, outdir):
    raw_props = load(f"{indir}/emoji-data.txt")
    props = {cp: set(v) for cp, v in raw_props.items()}
    raw_age = load(f"{indir}/DerivedAge.txt")
    age = {cp: tuple(int(x) for x in v[0].split(".")[:2]) for cp, v in raw_age.items()}

    pool = [cp for cp, pr in props.items()
            if "Emoji_Presentation" in pr and "Emoji_Modifier" not in pr
            and "Emoji_Component" not in pr and age.get(cp, (99, 99)) <= (13, 0)]
    assert len(pool) == 1113, f"pool={len(pool)} (expected 1113)"
    poolset = set(pool)

    # --- exclusion ruleset: explicit, auditable, sums to 89 ---
    DROP = {}
    def drop(reason, glyphs):
        for g in glyphs:
            cp = ord(g)
            assert cp in poolset, f"{g} U+{cp:04X} not in pool"
            DROP[cp] = reason

    drop("distinctness:clock-face",   [chr(c) for c in range(0x1F550, 0x1F568)])         # 24
    drop("distinctness:geom-near-dup", list("🔸🔹🔲🔳"))                                   # 4
    drop("text-boundary:script-box",  list("🆎🆑🆒🆓🆔🆕🆖🆗🆘🆙🆚🈁🈚🈯🈲🈳🈴🈵🈶🈸🈹🈺🉐🉑🔠🔡🔢🔣🔤"))  # 29
    drop("text-boundary:char-adjacent", list("❌❎❗❓❔❕➕➖➗⭕➰➿🔟"))                  # 13
    drop("offputting:prohibition",    list("⛔🚫🚭🚯🚱🚳🚷📵🔞"))                          # 9
    drop("offputting:gross-weapon",   list("🤮🔫🪓🔪🧨🖕🚬🤬"))                            # 8
    drop("offputting:gross-relief",   list("🦠🩸"))                                        # 2
    assert len(DROP) == 89, f"dropped={len(DROP)} (expected 89)"

    survivors = sorted((cp for cp in pool if cp not in DROP), key=lambda cp: (age[cp], cp))
    assert len(survivors) == 1024 and len(set(survivors)) == 1024
    # rendering invariant: the cluster range 0-783 must be entirely age<=9.0
    assert all(age[cp] <= (9, 0) for cp in survivors[:784]), "cluster range not all age<=9.0"
    for cp in survivors:
        assert "Emoji_Presentation" in props[cp] and age[cp] <= (13, 0)

    # group/subgroup metadata (annotation only)
    g = s = None; meta = {}
    for line in open(f"{indir}/emoji-test.txt"):
        t = line.strip()
        if t.startswith("# group:"): g = t.split(":", 1)[1].strip(); continue
        if t.startswith("# subgroup:"): s = t.split(":", 1)[1].strip(); continue
        if not t or t.startswith("#"): continue
        cps = line.split(";", 1)[0].strip().split()
        if len(cps) == 1: meta[int(cps[0], 16)] = (g, s)

    CHARSET = list("abcdefghijklmnopqrstuvwxyz.-")   # 28-char cluster alphabet, fixed order
    RANGES = [
        (0, 783,   "cluster",   "2-char clusters over [a-z . -]; index = c1*28 + c2"),
        (784, 811, "singleton", "one per cluster-alphabet char (forced boundary / final odd char)"),
        (812, 821, "digit",     "digit terminators 0-9 (opaque glyphs; legible digit glyphs are infeasible single-codepoint)"),
        (822, 838, "control",   "1 brand + route-subset + type-subset (17); meaning-assignment is Phase 3"),
        (839, 1023,"reserved",  "future expansion (185); glyphs fixed now, semantics later"),
    ]
    def role(i):
        for lo, hi, name, _ in RANGES:
            if lo <= i <= hi: return name

    alphabet = []
    for idx, cp in enumerate(survivors):
        try: name = unicodedata.name(chr(cp))
        except ValueError: name = "?"
        e = {"index": idx, "codepoint": f"U+{cp:04X}", "glyph": chr(cp), "name": name,
             "age": ".".join(map(str, age[cp])),
             "group": meta.get(cp, (None, None))[0], "subgroup": meta.get(cp, (None, None))[1],
             "role": role(idx)}
        if role(idx) == "cluster": e["cluster"] = CHARSET[idx // 28] + CHARSET[idx % 28]
        elif role(idx) == "singleton": e["char"] = CHARSET[idx - 784]
        elif role(idx) == "digit": e["digit"] = str(idx - 812)
        alphabet.append(e)

    canon = "".join(f"{e['index']}\t{e['codepoint']}\n" for e in alphabet).encode()
    digest = hashlib.sha256(canon).hexdigest()

    json.dump({"name": "encoji", "size": 1024, "age_max": "13.0", "sort": "age,codepoint",
               "sha256_index_codepoint": digest, "charset": "".join(CHARSET),
               "ranges": RANGES, "alphabet": alphabet},
              open(f"{outdir}/encoji-alphabet.json", "w"), ensure_ascii=False, indent=1)
    with open(f"{outdir}/encoji-alphabet.txt", "w") as f:
        for e in alphabet:
            extra = e.get("cluster") or e.get("char") or e.get("digit") or ""
            f.write(f"{e['index']:4d} {e['codepoint']:<8} {e['glyph']}  {e['role']:<9} {extra:<4} {e['name']}\n")
    with open(f"{outdir}/encoji-dropped.txt", "w") as f:
        for cp in sorted(DROP):
            try: nm = unicodedata.name(chr(cp))
            except ValueError: nm = "?"
            f.write(f"U+{cp:04X}\t{chr(cp)}\t{DROP[cp]}\t{nm}\n")

    print(f"alphabet size : {len(alphabet)}")
    print(f"sha256(idx+cp): {digest}")
    from collections import Counter
    print("dropped       :", dict(Counter(DROP.values())))
    return digest

if __name__ == "__main__":
    # usage: encoji_alphabet.py [INPUT_DIR=.] [OUTPUT_DIR=INPUT_DIR]
    indir = sys.argv[1] if len(sys.argv) > 1 else "."
    outdir = sys.argv[2] if len(sys.argv) > 2 else indir
    main(indir, outdir)
