#!/usr/bin/env python3
"""Golden-master fidelity harness for the live SKEIN (master).

This is a *characterization* gate, not a fell or a hardening pass. It pins the
behaviour of master AS IT IS NOW as the answer key, then proves that a future
master still produces the same output against the same frozen data. It exists
because a cutover once dropped capabilities silently (search filters, cross-site
activity, --since) and nothing caught it. This catches that *class* of drift.

It is a BLUNT instrument by design:
  - It runs ONE system: the legacy `skein` client/server (current master),
    against a FROZEN fixture, so the only variable between bless and check is the
    code. The dead content-hash station is gone; master characterizes itself.
  - It compares whole normalized command output, not just which records return,
    so it catches field/flag/ordering drift the old set-membership rig missed.
  - It does NOT judge correctness, ranking quality, or hash identity. It says
    "master changed its answer here" and shows you the diff to eyeball.

Two normalizations keep a green run meaningful:
  - content_hash values are blanked. Phase 0 deliberately rewrites them; a hash
    gate is the conformance tests' job, not this one.
  - long `content` bodies collapse to a char count. Body prose isn't a query
    behaviour and would bloat the baseline; a length change still shows.

Clock-relative probes (--since against frozen old data) drift on their own as
real time passes. They are captured but flagged TIME and reported as
informational — eyeball them, don't treat a diff as a regression.

Usage:
    python fidelity/harness.py bless     # capture current master as the baseline
    python fidelity/harness.py check     # diff current master against the baseline
    python fidelity/harness.py           # check (default)

Fixture override: FIDELITY_SOURCE=/path/to/legacy/.skein/data
Port override:    FIDELITY_PORT=8042
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
WORK = REPO / "fidelity" / ".work"
BASELINE = REPO / "fidelity" / "baseline"
FIXTURE_PROJECT = WORK / "legacy-project"          # legacy reads .skein/data here
# Default fixture: the frozen data the cutover archived. Override with $FIDELITY_SOURCE.
FIXTURE_SOURCE = Path(
    os.environ.get("FIDELITY_SOURCE", REPO / ".skein" / "legacy-archive-20260624" / "data")
)
LEGACY_PORT = int(os.environ.get("FIDELITY_PORT", "8042"))
LEGACY_URL = f"http://127.0.0.1:{LEGACY_PORT}"
FIX_ID = "fidelity-fixture"
REGISTRY = Path.home() / ".skein" / "projects.json"


# ── setup / teardown ────────────────────────────────────────────────────────

def build_fixture() -> None:
    """Copy the frozen legacy data into a throwaway project the server serves."""
    if not (FIXTURE_SOURCE / "skein.db").exists():
        sys.exit(f"fixture source missing a skein.db: {FIXTURE_SOURCE}")
    if WORK.exists():
        shutil.rmtree(WORK)
    data_dir = FIXTURE_PROJECT / ".skein" / "data"
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_SOURCE, data_dir)

    # Phase 2 read-flip: the frozen fixture has folios but no versions/refs, so the
    # join reads would hit empty tables and red the harness. Seed versions/refs
    # from the folios rows. The fixture is edit-free (heads == folios), so the
    # joined reads emit byte-identical output and the blessed baseline stays valid
    # — this is a backfill, NOT a re-bless.
    sys.path.insert(0, str(REPO))
    from skein.migrations.backfill_versions_refs import backfill_db
    backfill_db(data_dir / "skein.db", dry_run=False)


def register_fixture() -> Optional[str]:
    """Add the fixture to ~/.skein/projects.json. Returns the prior file text
    (or None if absent) so the caller can restore it verbatim."""
    prior = REGISTRY.read_text() if REGISTRY.exists() else None
    reg = json.loads(prior) if prior else {"projects": {}}
    reg.setdefault("projects", {})[FIX_ID] = {
        "path": str(FIXTURE_PROJECT),
        "data_dir": str(FIXTURE_PROJECT / ".skein" / "data"),
        "name": "Fidelity Fixture",
        "registered_at": "2026-06-24T00:00:00",
    }
    REGISTRY.write_text(json.dumps(reg, indent=2))
    return prior


def restore_registry(prior: Optional[str]) -> None:
    if prior is None:
        REGISTRY.unlink(missing_ok=True)
    else:
        REGISTRY.write_text(prior)


def start_legacy_server() -> subprocess.Popen:
    env = {**os.environ, "SKEIN_PORT": str(LEGACY_PORT)}
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "skein_server.py")],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Wait for the port, then for a real request to resolve the fixture project.
    for _ in range(80):  # ~16s
        if proc.poll() is not None:
            sys.exit("legacy server exited during startup")
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", LEGACY_PORT)) == 0:
                rc, _, _ = run_skein(["activity", "--json"])
                if rc == 0:
                    return proc
        time.sleep(0.2)
    proc.terminate()
    sys.exit("legacy server did not become ready")


# ── running a probe ─────────────────────────────────────────────────────────

def run_skein(args: List[str]) -> Tuple[int, str, str]:
    cmd = ["skein", "--url", LEGACY_URL, "--project", FIX_ID, *args]
    env = {**os.environ, "SKEIN_AGENT_ID": "fidelity"}
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, env=env)
    return r.returncode, r.stdout, r.stderr


def _normalize(obj: Any) -> Any:
    """Blank values that are non-deterministic between runs or change by design,
    recursively, preserving everything else and order. What gets blanked:
      - content_hash: rewritten by design in Phase 0; the conformance tests gate it.
      - content: long bodies bloat the baseline; keep only a char count.
      - per-run timing (*_ms, took/elapsed/duration): wall-clock noise.
      - active_agents: the live roster churns; out of scope for now (fix later)."""
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if k == "content_hash":
                out[k] = "<hash>"
            elif k == "content" and isinstance(v, str):
                out[k] = f"<{len(v)} chars>"
            elif k.endswith("_ms") or k in {"took", "elapsed", "duration"}:
                out[k] = "<ms>"
            elif k == "active_agents":
                out[k] = "<roster>"
            else:
                out[k] = _normalize(v)
        return out
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    return obj


def capture(args: List[str]) -> str:
    """Run a probe and return its canonical, normalized, diffable text."""
    rc, out, err = run_skein(args)
    header = f"# argv: {' '.join(args)}\n# rc: {rc}\n"
    body: str
    try:
        body = json.dumps(_normalize(json.loads(out)), indent=2, sort_keys=True,
                          ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        # Non-JSON probe (e.g. --oneline) or empty: keep stdout verbatim, and
        # surface stderr's first line so an error shape is visible in the diff.
        body = out.rstrip("\n")
        if rc != 0:
            first = next((ln for ln in err.splitlines() if ln.strip()), "")
            body += f"\n# stderr: {first[:200]}"
    return header + body + "\n"


# ── the corpus ──────────────────────────────────────────────────────────────
# kind: "stable" — deterministic against the frozen fixture; a diff is a regression.
#       "time"   — clock-relative (--since); drifts on its own, diff is informational.
PROBES: List[Dict[str, Any]] = [
    # find — the unified query surface (this is where drift hides)
    {"name": "find-all",            "kind": "stable", "argv": ["find", "--limit", "500", "--json"]},
    {"name": "find-type-brief",     "kind": "stable", "argv": ["find", "--type", "brief", "--limit", "500", "--json"]},
    {"name": "find-type-issue",     "kind": "stable", "argv": ["find", "--type", "issue", "--limit", "500", "--json"]},
    {"name": "find-type-finding",   "kind": "stable", "argv": ["find", "--type", "finding", "--limit", "500", "--json"]},
    {"name": "find-status-open",    "kind": "stable", "argv": ["find", "--status", "open", "--limit", "500", "--json"]},
    {"name": "find-status-closed",  "kind": "stable", "argv": ["find", "--status", "closed", "--limit", "500", "--json"]},
    {"name": "find-site-exact",     "kind": "stable", "argv": ["find", "--site", "mesh-v1", "--limit", "500", "--json"]},
    {"name": "find-site-wildcard",  "kind": "stable", "argv": ["find", "--site", "mesh-*", "--limit", "500", "--json"]},
    {"name": "find-text",           "kind": "stable", "argv": ["find", "security", "--limit", "500", "--json"]},
    {"name": "find-text-typed",     "kind": "stable", "argv": ["find", "bug", "--type", "issue", "--limit", "500", "--json"]},
    {"name": "find-sort-asc",       "kind": "stable", "argv": ["find", "--sort", "created_asc", "--limit", "500", "--json"]},
    # find-archived probe RETIRED 2026-07-08: the --archived flag was removed
    # with the folio-archived feature (threads-only contraction; zero uses
    # ecosystem-wide). A deliberate capability drop, recorded here so the probe
    # corpus change is conscious — any blessed find-archived baseline is stale.

    # search — the other engine (FTS word-match), with its filter flags
    {"name": "search-basic",        "kind": "stable", "argv": ["search", "mesh", "--limit", "500", "--json"]},
    {"name": "search-type",         "kind": "stable", "argv": ["search", "mesh", "--type", "brief", "--limit", "500", "--json"]},
    {"name": "search-status",       "kind": "stable", "argv": ["search", "mesh", "--status", "open", "--limit", "500", "--json"]},
    {"name": "search-site",         "kind": "stable", "argv": ["search", "mesh", "--site", "mesh-v1", "--limit", "500", "--json"]},
    {"name": "search-sites-wild",   "kind": "stable", "argv": ["search", "mesh", "--sites", "mesh-*", "--limit", "500", "--json"]},
    {"name": "search-resources",    "kind": "stable", "argv": ["search", "mesh", "--resources", "folios,threads", "--limit", "500", "--json"]},

    # folios (deprecated alias of find --site, still in the surface)
    {"name": "folios-site",         "kind": "stable", "argv": ["folios", "mesh-v1", "--all", "--json"]},
    {"name": "folios-status",       "kind": "stable", "argv": ["folios", "mesh-v1", "--status", "open", "--all", "--json"]},
    {"name": "folios-type",         "kind": "stable", "argv": ["folios", "mesh-v1", "--type", "brief", "--all", "--json"]},

    # issues / frictions list verbs
    {"name": "issues-all",          "kind": "stable", "argv": ["issues", "--json"]},
    {"name": "issues-open",         "kind": "stable", "argv": ["issues", "--status", "open", "--json"]},
    {"name": "issues-site",         "kind": "stable", "argv": ["issues", "mesh-v1", "--json"]},
    {"name": "frictions-all",       "kind": "stable", "argv": ["frictions", "--json"]},
    {"name": "frictions-site",      "kind": "stable", "argv": ["frictions", "mesh-v1", "--json"]},

    # sites / status / stats — aggregates and counts
    {"name": "sites",               "kind": "stable", "argv": ["sites", "--json"]},
    {"name": "status",              "kind": "stable", "argv": ["status", "--json"]},
    {"name": "stats-folios",        "kind": "stable", "argv": ["stats", "folios", "--json"]},
    {"name": "stats-folios-type",   "kind": "stable", "argv": ["stats", "folios", "--by-type", "--json"]},
    {"name": "stats-folios-status", "kind": "stable", "argv": ["stats", "folios", "--by-status", "--json"]},
    {"name": "stats-folios-site",   "kind": "stable", "argv": ["stats", "folios", "--by-site", "--json"]},
    {"name": "stats-threads",       "kind": "stable", "argv": ["stats", "threads", "--json"]},
    {"name": "stats-threads-weaver","kind": "stable", "argv": ["stats", "threads", "--by-weaver", "--json"]},
    {"name": "stats-threads-orphan","kind": "stable", "argv": ["stats", "threads", "--orphaned", "--json"]},

    # log — git-style history with its filters
    {"name": "log",                 "kind": "stable", "argv": ["log", "--limit", "100", "--no-pager", "--json"]},
    {"name": "log-type",            "kind": "stable", "argv": ["log", "--type", "brief", "--limit", "100", "--no-pager", "--json"]},
    {"name": "log-grep",            "kind": "stable", "argv": ["log", "--grep", "security", "--limit", "100", "--no-pager", "--json"]},
    {"name": "log-oneline",         "kind": "stable", "argv": ["log", "--limit", "50", "--oneline", "--no-pager"]},

    # threads — the edge surface
    {"name": "threads-type-status", "kind": "stable", "argv": ["threads", "--type", "status", "--json"]},

    # activity — recent feed (no --since: ranks by stored date, stable on frozen data)
    {"name": "activity",            "kind": "stable", "argv": ["activity", "--json"]},

    # clock-relative — informational, eyeball only
    {"name": "find-since-1day",     "kind": "time", "argv": ["find", "--since", "1day", "--limit", "500", "--json"]},
    {"name": "log-since-1day",      "kind": "time", "argv": ["log", "--since", "1day", "--limit", "100", "--no-pager", "--json"]},
    {"name": "activity-since-1day", "kind": "time", "argv": ["activity", "--since", "1day", "--json"]},
    {"name": "threads-since-1day",  "kind": "time", "argv": ["threads", "--since", "1day", "--json"]},
]


# ── modes ───────────────────────────────────────────────────────────────────

def _with_live_system(fn):
    """Build the fixture, boot legacy against it, run fn(), always tear down."""
    prior = None
    server = None
    try:
        build_fixture()
        prior = register_fixture()
        server = start_legacy_server()
        return fn()
    finally:
        if server is not None:
            try:
                os.killpg(os.getpgid(server.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        restore_registry(prior)


def bless() -> None:
    print("Fidelity harness — BLESS (capturing current master as the baseline)")
    print(f"fixture source: {FIXTURE_SOURCE}")
    print("setting up (fixture copy, legacy server)...\n")

    def _run() -> Dict[str, str]:
        return {p["name"]: capture(p["argv"]) for p in PROBES}

    captures = _with_live_system(_run)

    if BASELINE.exists():
        shutil.rmtree(BASELINE)
    BASELINE.mkdir(parents=True)
    for name, text in captures.items():
        (BASELINE / f"{name}.txt").write_text(text)
    print(f"blessed {len(captures)} probes -> {BASELINE.relative_to(REPO)}")
    print("commit this baseline; future `check` runs diff against it.")


def check() -> None:
    if not BASELINE.exists():
        sys.exit("no baseline; run `python fidelity/harness.py bless` first.")

    print("Fidelity harness — CHECK (current master vs blessed baseline)")
    print(f"fixture source: {FIXTURE_SOURCE}")
    print("setting up (fixture copy, legacy server)...\n")

    def _run() -> Dict[str, str]:
        return {p["name"]: capture(p["argv"]) for p in PROBES}

    captures = _with_live_system(_run)
    kinds = {p["name"]: p["kind"] for p in PROBES}

    drift_stable: List[str] = []
    drift_time: List[str] = []
    missing_baseline: List[str] = []

    for name, now in captures.items():
        bpath = BASELINE / f"{name}.txt"
        if not bpath.exists():
            missing_baseline.append(name)
            continue
        was = bpath.read_text()
        if was == now:
            continue
        diff = "".join(difflib.unified_diff(
            was.splitlines(keepends=True), now.splitlines(keepends=True),
            fromfile=f"baseline/{name}", tofile=f"now/{name}",
        ))
        bucket = drift_time if kinds.get(name) == "time" else drift_stable
        bucket.append(name)
        tag = "TIME " if kinds.get(name) == "time" else "DRIFT"
        print(f"=== {tag} {name} ===")
        print(diff if len(diff) < 6000 else diff[:6000] + "\n... (diff truncated)\n")

    # baseline probes that vanished from the corpus
    stale = [p.stem for p in BASELINE.glob("*.txt") if p.stem not in captures]

    print("\n── summary ──")
    print(f"probes: {len(captures)}  stable-drift: {len(drift_stable)}  "
          f"time-drift (informational): {len(drift_time)}  "
          f"new-probe-no-baseline: {len(missing_baseline)}  "
          f"stale-baseline: {len(stale)}")
    if drift_stable:
        print("REGRESSION — stable probes changed:", ", ".join(drift_stable))
    if drift_time:
        print("time-relative (eyeball, expected to move):", ", ".join(drift_time))
    if missing_baseline:
        print("no baseline (re-bless to adopt):", ", ".join(missing_baseline))
    if stale:
        print("stale baseline (probe removed):", ", ".join(stale))
    if not drift_stable:
        print("\nOK — no stable drift.")
    sys.exit(1 if drift_stable else 0)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "bless":
        bless()
    elif mode == "check":
        check()
    else:
        sys.exit(f"unknown mode {mode!r}; use 'bless' or 'check'.")


if __name__ == "__main__":
    main()
