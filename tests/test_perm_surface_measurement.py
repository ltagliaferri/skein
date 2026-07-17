"""S13 — the surface MEASUREMENT (brief-20260716-93t3 §2, §5 Workstream 0; S13 §7).

The plan's keystone, and the display/type design pair's ground truth
(brief-20260717-7mdt). It answers ONE question empirically, not by static derivation:
*when a bound-but-UNAUTHORIZED party publishes one thread of each type onto a victim's
lineage, which land, and where do they render?*

WHY MEASURE, NOT DERIVE. Five static derivations of this surface were wrong — twice by
the very instrument built to stop it (§2). A literal-grep audit declared ``tag`` carries
no read power; the generic renderer proves it renders. So this does not read the source:
it lands a real thread through real ``ingest()`` at rung 2 (:mod:`tests.perm_multiparty`)
and DIFFS the rendered ``build_folio_envelope`` surface before and after. Immune to
literal-greps, to set-membership reasoning, and to the reader list being incomplete.

THE FIXTURE CARRIES THE STATE THE BLAST RADIUS NEEDS (§2). A surface that only exists
under state a single landed thread does not create is invisible to the diff — so the
victim lineage carries a sibling, a descendant chain, a site membership, and a superseded
head. This is the one place static reading beats measurement (``_folio_lineage`` names
``siblings`` regardless of state), so the two are complements: measure the surface, build
the state that surface needs.

THE RENDER SET (§2, the residual named as one). This diffs the HTTP folio envelope
(``build_folio_envelope``: threads_in/out, lineage children/siblings/parents, descendants,
superseded_by, site) AND the catalog's ``within`` reader (``folio_site_slugs``, the "sixth
reader" a naive query misses, station_store.py). The catalog surface is keyed by MEMBER
HASH, not title, so an admitted ``within`` is detectable there — proven by the positive
control ``test_catalog_within_reader_fires_on_admitted_membership`` (in the sweep itself
``within`` is refused, so its catalog signal is only ever asserted ABSENT). NOT measured
here, and named as a list rather than papered over: the non-HTTP renderers ``render.py`` /
``mesh/`` / ``resolve.py`` / ``station_cli.py`` (§2). ``published`` is WIRE_REJECT — it
can never land, so 16 of 17 are landably-measurable and its flag stays unmeasured.

THE VERDICT PARTITION. Post the assignment/attribution to-end fix (b77af99, live), the
y297-relevant numbers are 6 and 3, not 8 and 4: six types inject into ``threads_in`` with
attacker-chosen text, of which three are declared ``reads=False`` while rendering anyway
(finding-20260716-oz4t finding 2). This is a DELIVERABLE for y297, not a verdict on the
model (claim 3 is unfalsifiable while §9's Q5 is open).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Set, Tuple

import pytest

from skein.envelope import build_folio_envelope
from skein.identity import compute_thread_hash
from skein.station import Station
from skein.thread_authz import classify_thread, known_thread_types, thread_reads
from tests import perm_multiparty as mp
from tests import station_publish_helpers as h

# Fixture timestamps stay strictly before the attack timestamp so every BOB edge has a
# novel thread_hash (never an idempotent collision with a fixture edge).
_TS = {
    "site": "2026-01-01T00:00:00+00:00",
    "g": "2026-01-02T00:00:00+00:00",
    "wg": "2026-01-03T00:00:00+00:00",
    "v": "2026-01-04T00:00:00+00:00",
    "wv": "2026-01-05T00:00:00+00:00",
    "v2": "2026-01-06T00:00:00+00:00",
    "s": "2026-01-07T00:00:00+00:00",
}
BOB_TS = "2026-06-01T00:00:00+00:00"


def _thread(ttype: str, frm: str, to: str, created_at: str = BOB_TS) -> Dict[str, Any]:
    """A wire thread row (content-addressed thread_hash + canonical fields)."""
    th = compute_thread_hash(
        from_id=frm, to_id=to, type=ttype, weaver=None, created_at=created_at, content=None,
    )
    return {
        "thread_hash": th, "from_id": frm, "to_id": to, "type": ttype,
        "weaver": None, "created_at": created_at, "content": None,
    }


class Fixture:
    """The victim corpus, owned entirely by ALICE. Hashes are named for the sweep specs.

    Topology (all ALICE-signed, so ``owns_folio``/``lineage_owner`` resolve to ALICE):

        G   genesis, member of siteA           <- the lineage the attacks target
        V   supersedes G, member of siteA      <- a superseded head (V2 supersedes it)
        V2  supersedes V                       <- descendant chain; C, a folio never named
        S   supersedes G                       <- V's sibling (co-child of G)
        siteA  site folio, slug "victim-site"  <- the multi-site membership surface
    """

    def __init__(self, hashes: Dict[str, str]):
        self.__dict__.update(hashes)
        self.victims = [self.G, self.V, self.V2, self.S]
        # A stable per-hash label so a diff reads "V2.siblings" not a bare hash.
        self.label = {self.G: "G", self.V: "V", self.V2: "V2", self.S: "S", self.siteA: "siteA"}


def build_victim_fixture(instance) -> Fixture:
    """Publish the victim corpus AS ALICE through the real ON path, then bind BOB."""
    mp.bind_party(instance, mp.ALICE, "originator")
    mp.bind_party(instance, mp.BOB, "originator")

    siteA = h.folio("site", "Victim Site", "the victim's site", _TS["site"])
    G = h.folio("finding", "victim genesis", "g", _TS["g"])
    V = h.folio("finding", "victim head v2", "v", _TS["v"])
    V2 = h.folio("finding", "victim head v3", "v3", _TS["v2"])
    S = h.folio("finding", "sibling fork", "s", _TS["s"])
    sh = {k: f["content_hash"] for k, f in
          {"siteA": siteA, "G": G, "V": V, "V2": V2, "S": S}.items()}

    def within(frm, to, ts):
        return _thread("within", sh[frm], sh[to], ts)

    def supersedes(new, old, ts):
        return _thread("supersedes", sh[new], sh[old], ts)

    steps = [
        ([siteA], [], {sh["siteA"]: "victim-site"}),
        ([G], [within("G", "siteA", _TS["wg"])], None),
        ([V], [supersedes("V", "G", _TS["v"]), within("V", "siteA", _TS["wv"])], None),
        ([V2], [supersedes("V2", "V", _TS["v2"])], None),
        ([S], [supersedes("S", "G", _TS["s"])], None),
    ]
    for folios, threads, slugs in steps:
        ack = mp.publish_as(instance, mp.ALICE, folios, threads, site_slugs=slugs)
        assert not ack["rejected"], ack["rejected"]
        assert not ack["threads"]["rejected"], ack["threads"]["rejected"]
    return Fixture(sh)


def render_surface(store, fix: Fixture) -> Set[str]:
    """Snapshot every place an injected TITLE could surface, as ``label:surface:title``.

    Covers the folio envelope's cross-reference/lineage/site surfaces for each victim
    AND the catalog's ``within`` reader (``folio_site_slugs``) — the surface set the plan
    names, minus the non-HTTP renderers it names as an unmeasured list.
    """
    surface: Set[str] = set()

    def peers(label: str, name: str, refs: List[Mapping[str, Any]]):
        for r in refs:
            surface.add(f"{label}:{name}:{r.get('title')}")

    for vh in fix.victims:
        env = build_folio_envelope(store, vh)["asserted"]
        label = fix.label[vh]
        peers(label, "threads_in", env["threads_in"])
        peers(label, "threads_out", env["threads_out"])
        peers(label, "children", env["lineage"]["children"])
        peers(label, "siblings", env["lineage"]["siblings"])
        peers(label, "parents", env["lineage"]["parents"])
        peers(label, "descendants", env["descendants"])
        sup = env["superseded_by"]  # the fork "a newer version exists" hatnote (render.py)
        if sup:
            surface.add(f"{label}:superseded_by:{sup.get('title')}")
        site = env["site"]
        if site:
            surface.add(f"{label}:site:{site['slug']}")
    # The catalog within-reader: member folio -> site slug (a landed within shows here).
    for folio_hash, slug in store.folio_site_slugs().items():
        surface.add(f"catalog:within:{fix.label.get(folio_hash, folio_hash)}->{slug}")
    return surface


# --- the sweep: one attack per type, from BOB the unauthorized bound originator ---------
# (ttype, from_key, to_key, expect_admit, reason_substr, anchor_key). Keys resolve against
# the built fixture; "bob_own" is a fresh folio BOB owns (so his from-end ownership is
# genuine). anchor_key names the fixture hash the refusal must cite — asserting it binds
# the reject to the RESOLVED lineage/site anchor, so a resolver that gated on the head
# instead of the genesis would fail even though the class phrase still matched (§3 rule 1).
ATTACK_SPECS: List[Tuple[str, str, str, bool, str, Optional[str]]] = [
    # AFFECTING — from-end ownership + to-end right; BOB has neither on the victim.
    #   supersedes: BOB owns his new version but not the target genesis -> to-end refusal.
    #   within: BOB owns his member folio but holds no site-contribute right -> to-end
    #   refusal at the site GENESIS (this exercises the folio_site_slugs catalog reader;
    #   filing the victim's OWN folio would instead reject at the from-end and never reach
    #   the site logic).
    ("supersedes", "bob_own", "V", False, "no edit access", "G"),
    ("within", "bob_own", "siteA", False, "no contribute access", "siteA"),
    # POINTER — from-end ownership ONLY, no to-end right (by design): all admit + render.
    ("reference", "bob_own", "V", True, "", None),
    ("reply", "bob_own", "V", True, "", None),
    ("mention", "bob_own", "V", True, "", None),
    ("forks", "bob_own", "V", True, "", None),
    ("responds_to", "bob_own", "V", True, "", None),
    ("imports_legacy", "bob_own", "V", True, "", None),
    ("tag", "bob_own", "V", True, "", None),
    # CONTROL — to-end moderation right at the genesis; BOB has none.
    ("status", "G", "G", False, "no moderation access", "G"),
    ("reverted", "V2", "G", False, "no moderation access", "G"),
    ("archive", "G", "G", False, "no moderation access", "G"),
    # ASSIGNMENT / ATTRIBUTION — content-hash to-end now shape-rejected (b77af99, live).
    ("assignment", "bob_own", "V", False, "assignment to-end must name an agent", None),
    ("attribution", "bob_own", "V", False, "attribution to-end must name an agent", None),
    # NON_FOLIO — admits from a held folio BOB owns (the deferred message/succession pair).
    ("message", "bob_own", "V", True, "", None),
    ("succession", "bob_own", "V", True, "", None),
    # WIRE_REJECT — never travels the published graph.
    ("published", "bob_own", "V", False, "does not travel the published graph", None),
]


class AttackResult:
    def __init__(self, ttype, admitted, reason, injected_surfaces, bob_title, anchor_hash):
        self.ttype = ttype
        self.admitted = admitted
        self.reason = reason
        self.injected_surfaces = injected_surfaces  # the label:surface:title deltas
        self.bob_title = bob_title
        self.anchor_hash = anchor_hash  # the resolved genesis/site the refusal must cite


def run_attack(instance, fix: Fixture, spec) -> AttackResult:
    """BOB publishes a fresh owned folio + one thread of ``spec`` onto the victim,
    then diff the rendered surface. Returns admission + reason + injected surfaces."""
    ttype, from_key, to_key, _admit, _reason, anchor_key = spec
    bob_title = f"BOB-INJECT-{ttype}"
    bob_own = h.folio("finding", bob_title, "payload", BOB_TS)
    bob_hash = bob_own["content_hash"]
    resolve = {"bob_own": bob_hash, **{k: getattr(fix, k) for k in
               ("G", "V", "V2", "S", "siteA")}}
    edge = _thread(ttype, resolve[from_key], resolve[to_key])

    before = render_surface(instance.store, fix)
    ack = mp.publish_as(instance, mp.BOB, [bob_own], [edge])
    after = render_surface(instance.store, fix)

    th = edge["thread_hash"]
    admitted = th in ack["threads"]["accepted"]
    reason = ""
    if not admitted:
        rej = [r for r in ack["threads"]["rejected"] if r["thread_hash"] == th]
        reason = rej[0]["reason"] if rej else "<not in ack>"
    # A surface is BOB's iff it carries his title (folio-envelope peers resolve a title)
    # OR his content hash (the catalog within-reader keys on the member HASH, no title —
    # so a title-only filter would be BLIND to an admitted `within`, the sixth reader).
    injected = {s for s in (after - before) if bob_title in s or bob_hash in s}
    anchor_hash = getattr(fix, anchor_key) if anchor_key else None
    return AttackResult(ttype, admitted, reason, injected, bob_title, anchor_hash)


@pytest.fixture(scope="module")
def measured(tmp_path_factory) -> Dict[str, AttackResult]:
    """Run the whole sweep ONCE (each type in its own fresh station) and share the map
    across every check — the per-type asserts and the aggregate partition read the same
    measurement, so the real ingest+render sweep runs a single time per session."""
    out: Dict[str, AttackResult] = {}
    for spec in ATTACK_SPECS:
        d = tmp_path_factory.mktemp(f"surf_{spec[0]}")
        inst = Station(d / ".skein-next")
        try:
            fix = build_victim_fixture(inst)
            out[spec[0]] = run_attack(inst, fix, spec)
        finally:
            inst.close()
    return out


@pytest.mark.parametrize("spec", ATTACK_SPECS, ids=[s[0] for s in ATTACK_SPECS])
def test_surface_per_type(measured, spec):
    """One thread of each type, landed by an unauthorized bound party. Admission and the
    SPECIFIC reject reason are both frozen (a right-answer-wrong-reason refusal is a test
    bug wearing a pass, §3 rule 1)."""
    ttype, _fk, _tk, expect_admit, reason_substr, anchor_key = spec
    res = measured[ttype]

    assert res.admitted is expect_admit, (
        f"{ttype}: admitted={res.admitted} expected {expect_admit}; reason={res.reason!r}"
    )
    if expect_admit:
        # Admitted => it renders the attacker's title somewhere on the victim surface.
        assert res.injected_surfaces, f"{ttype} admitted but rendered nowhere (silent?)"
    else:
        assert reason_substr in res.reason, (
            f"{ttype}: reason {res.reason!r} lacks {reason_substr!r}"
        )
        # The refusal must cite the RESOLVED anchor (genesis/site), not just the class
        # phrase — a resolver that gated on the head would still match the phrase.
        if anchor_key is not None:
            assert res.anchor_hash and res.anchor_hash in res.reason, (
                f"{ttype}: reason {res.reason!r} does not cite the resolved "
                f"{anchor_key} anchor {res.anchor_hash}"
            )
        # Refused => the attacker's title reaches NO victim surface.
        assert not res.injected_surfaces, (
            f"{ttype} refused but still rendered at {res.injected_surfaces}"
        )


def test_surface_partition(measured):
    """The aggregate map + the y297 partition (post-fix 6 and 3), emitted for the design
    pair. This is the S13 deliverable — a measured type->surface map, not a derivation."""
    results = measured

    # The sweep must cover the WHOLE taxonomy — else a new type added to the renderer +
    # known_thread_types() would go silently unmeasured (the staleness §2 warns about).
    assert {s[0] for s in ATTACK_SPECS} == known_thread_types(), (
        "ATTACK_SPECS drifted from the taxonomy: "
        f"{known_thread_types() ^ {s[0] for s in ATTACK_SPECS}}"
    )

    admitted = {t for t, r in results.items() if r.admitted}
    rejected = {t for t, r in results.items() if not r.admitted}
    assert admitted == {
        "reference", "reply", "mention", "forks", "responds_to", "imports_legacy",
        "tag", "message", "succession",
    }, admitted
    assert rejected == {
        "supersedes", "within", "status", "reverted", "archive",
        "assignment", "attribution", "published",
    }, rejected

    # Partition the admitted types by WHERE they inject.
    def surfaces_of(t):
        return {s.split(":", 2)[1] for s in results[t].injected_surfaces}

    threads_in = {t for t in admitted if "threads_in" in surfaces_of(t)}
    lineage = {t for t in admitted if surfaces_of(t) & {"children", "siblings", "descendants"}}
    assert threads_in == {"reference", "reply", "mention", "tag", "message", "succession"}, threads_in
    assert lineage == {"forks", "responds_to", "imports_legacy"}, lineage

    # The y297 landmine: types that RENDER but are declared reads=False (finding 2).
    reads_false_but_render = {t for t in threads_in if not thread_reads(t)}
    assert reads_false_but_render == {"tag", "message", "succession"}, reads_false_but_render

    # The headline blast radius: one forks edge naming only V renders on V2.siblings —
    # a folio ALICE never named (§2). Measured, not asserted from the renderer. Assert
    # BOTH the sibling hop AND the descendants walk independently, so a silent break in
    # either surface is caught (children+siblings alone would otherwise mask a dead walk).
    forks_surfaces = results["forks"].injected_surfaces
    assert any(s.startswith("V2:siblings:") for s in forks_surfaces), forks_surfaces
    assert any(":descendants:" in s for s in forks_surfaces), forks_surfaces

    # Emit the measured map + class table for the design pair (visible under -s).
    wire_reject = {t for t in known_thread_types() if classify_thread(t).value == "wire_reject"}
    print("\n=== S13 measured type -> surface map (rung 2, post-b77af99) ===")
    print(f"swept: {len(ATTACK_SPECS)} of {len(known_thread_types())} taxonomy types; "
          f"landable: {len(ATTACK_SPECS) - len(wire_reject)} "
          f"(WIRE_REJECT never lands: {sorted(wire_reject)})")
    print(f"INJECT via threads_in ({len(threads_in)}): {sorted(threads_in)}")
    print(f"  of which declared reads=False ({len(reads_false_but_render)}): "
          f"{sorted(reads_false_but_render)}")
    print(f"INJECT via version chain ({len(lineage)}): {sorted(lineage)}")
    print(f"REFUSED ({len(rejected)}): {sorted(rejected)}")
    for t in sorted(admitted):
        print(f"  ADMIT {t:16} class={classify_thread(t).value:11} "
              f"reads={thread_reads(t)!s:5} -> {sorted(surfaces_of(t))}")
    for t in sorted(rejected):
        print(f"  REJECT {t:15} class={classify_thread(t).value:11} :: {results[t].reason}")


def test_catalog_within_reader_fires_on_admitted_membership(instance):
    """POSITIVE control for the sixth reader (opus fell). In the sweep, ``within`` is
    always REFUSED, so the catalog ``folio_site_slugs`` detector is only ever asserted
    ABSENT — an absent signal proves nothing about whether the detector can fire. Here
    membership is legitimately ADMITTED (BOB granted ``site_contribute``), so the catalog
    surface MUST register BOB's folio, keyed by HASH not title. This demonstrates the
    detection the sweep relies on actually works — the surface half of the measurement
    is not blind to the membership axis."""
    fix = build_victim_fixture(instance)
    bob_own = h.folio("finding", "BOB-MEMBER", "payload", BOB_TS)
    bob_hash = bob_own["content_hash"]
    mp.grant_on(instance, fix.siteA, mp.BOB, "site_contribute")
    edge = _thread("within", bob_hash, fix.siteA)

    before = render_surface(instance.store, fix)
    ack = mp.publish_as(instance, mp.BOB, [bob_own], [edge])
    after = render_surface(instance.store, fix)

    assert edge["thread_hash"] in ack["threads"]["accepted"], ack["threads"]["rejected"]
    catalog_delta = {s for s in (after - before) if s.startswith("catalog:within:")}
    assert any(bob_hash in s for s in catalog_delta), (
        f"admitted within did not register on the catalog folio_site_slugs reader: "
        f"{sorted(catalog_delta)}"
    )


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein-next")
    yield s
    s.close()
