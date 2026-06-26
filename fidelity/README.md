# Fidelity harness

A **golden-master characterization gate** for the live SKEIN (master). It pins
master's behaviour AS IT IS NOW as the answer key, then proves a future master
still produces the same output against the same frozen data. It exists because a
cutover once dropped capabilities silently — search filters, cross-site
activity, `--since` — and nothing caught it. This catches that *class* of drift.

It is **not** a fell or a hardening pass. It is a blunt instrument: it tells you
master changed its answer and shows you the diff. It does not judge correctness.

## How it works

One system, not two. The dead content-hash station is gone; master
characterizes itself.

1. Copies a **frozen** legacy project's data into a throwaway fixture (so the
   only variable between bless and check is the code, never the data).
2. Boots the legacy server against that fixture (own port, own registry entry).
3. Runs a corpus of read commands and captures each one's whole normalized
   output — not just which records return, so field/flag/ordering drift shows.

## Run

    python fidelity/harness.py bless     # capture current master as the baseline
    python fidelity/harness.py check     # diff current master against the baseline
    python fidelity/harness.py           # check (default)

`bless` writes one file per probe under `fidelity/baseline/` (committed). `check`
diffs the current run against it, prints a unified diff per changed probe, and
**exits non-zero** if any stable probe drifted (so it can gate). Re-bless only
when you change behaviour on purpose.

Fixture override: `FIDELITY_SOURCE=/path/to/legacy/.skein/data`. Port:
`FIDELITY_PORT`. The registry edit and the server process are both undone in a
`finally` block; the throwaway fixture lives in the git-ignored `.work/`.

## What is normalized out (so a green run is meaningful)

- `content_hash` — rewritten by design in Phase 0; the conformance tests gate it.
- `content` bodies — collapsed to a char count; prose isn't a query behaviour
  and would bloat the baseline. A length change still shows.
- per-run timing (`*_ms`, took/elapsed/duration) — wall-clock noise.
- `active_agents` — the live roster churns; out of scope for now.

Clock-relative probes (`--since`) drift on their own as real time passes. They
are captured but flagged `TIME` and reported as **informational** — eyeball
them, a diff there is not a regression.

## What it proves — and what it does NOT

It proves, for each probed read command, that master returns the same records
with the same fields in the same order against the same data.

It does **not**:

- Verify hash identity (that is the conformance tests' job).
- Compare content body text beyond its length.
- Judge ranking quality or correctness — only equality-to-baseline.
- Cover the whole surface. Legacy is ~95 verbs; this corpus is the query and
  discovery cluster (find, search, folios, issues, frictions, sites, status,
  stats, log, threads, activity) with their filter flags — the highest-risk
  slice, not the full gate. Mutating verbs are deliberately excluded (they would
  change the fixture and break re-run determinism).

## Extending

Add an entry to `PROBES`: `{name, kind, argv}`. `kind` is `stable` (a diff is a
regression) or `time` (clock-relative, informational). Use `--json` where the
verb supports it so the output normalizes cleanly. Re-bless to adopt it.
