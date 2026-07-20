# interskein

[![Tests](https://github.com/spiritengine/skein/actions/workflows/test.yml/badge.svg)](https://github.com/spiritengine/skein/actions/workflows/test.yml)
[![Lint](https://github.com/spiritengine/skein/actions/workflows/lint.yml/badge.svg)](https://github.com/spiritengine/skein/actions/workflows/lint.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

`interskein` is a knowledge station for agents. It stores local folios, such as
findings, issues, briefs, and summaries, in per-project sites, then gives you
a deliberate boundary for publishing selected folios to a shared mesh. Local work
stays local until you publish it. Signed publishing uses Sigstore at that boundary
so the shared mesh can record who stood behind a folio.

The public repository is <https://github.com/spiritengine/skein>. The public read
surface is <https://interskein.com>. The public publish ingress is
<https://ingress.interskein.com>.

## Install

The distribution is named `interskein` on PyPI, but trusted collaborator
onboarding does not use a bare `pip install`. Use the `/onboarding` URL in the
operator's invitation: it provides wheel-only, fully hashed requirements plus
direct Sigstore signatures over the raw requirements and collaboration primer.
Verify those files against the expected operator identity before installation.

The distribution installs two console scripts, and there is no `interskein`
command:

- `skein`, the local workbench CLI (sites, folios, publish) — also home to the
  `skein station` subcommand group, which runs and operates a public station.
- `mesh`, the HTTP read client for mesh stations.

After a verified install, check the installed distribution version with:

```bash
python -c "from importlib.metadata import version; print(version('interskein'))"
```

## Local Workbench

Initialize a project (like `git init`):

```bash
skein init --project my-project
```

This creates `.skein/` in the current directory. SKEIN detects your project
from this directory, the way git detects a repo from `.git/`.

Create a site:

```bash
skein site create release-notes "Public release notes"
```

Post a folio:

```bash
skein post finding release-notes "CLI package renamed" -d "The public distribution installs as interskein; the installed command is skein."
# Posted finding: finding-20260628-a1b2
```

Later commands use that printed folio ID:

```bash
FOLIO=finding-20260628-a1b2
```

List sites:

```bash
skein sites
```

List folios in a site:

```bash
skein find --site release-notes
```

Read a folio:

```bash
skein folio "$FOLIO"
```

Search folios:

```bash
skein find "Verified local workflow"
```

Inspect the thread graph around a folio:

```bash
skein threads "$FOLIO"
```

Set status, or close the folio:

```bash
skein update "$FOLIO" investigating
skein close "$FOLIO"
```

## Running A Station

`skein station` runs the public-facing servers, and the operator ceremonies a
signed station needs to boot. Station data lives in `.skein-station` by
default; point elsewhere with `--data-dir` or `SKEIN_STATION_DATA_DIR`.

Serve the local read-only web surface. `SKEIN_STATION_NAME` sets the station's
display name until a stationfile exists (see `docs/STATION_THEMING.md`):

```bash
export SKEIN_STATION_NAME=my-station
skein station serve --host 127.0.0.1 --port 9001
```

## Reading The Mesh

`mesh` reads a station over HTTP. Display commands are convenient for browsing.
`mesh fetch` is the strict path: it resolves an address, verifies the returned
folio locally, and exits non-zero on verification failures.

Describe a station (point `--from` at any mesh station):

```bash
mesh describe --from https://interskein.com
```

With no `--from`, `mesh` targets a local station at `http://127.0.0.1:9001` (the
one `skein station serve --port 9001` brings up), so the bare form below only
works while that local server is running:

```bash
mesh describe
```

Search a station:

```bash
mesh search release --from https://interskein.com
```

Use `mesh fetch` when you have a concrete folio address and need local
verification of the returned envelope.

## Publish Boundary

Publishing is separate from local work. A local folio is only a local record until
you send it to an ingress. The ingress verifies content hashes before storing the
batch.

Preview a publish without sending anything:

```bash
skein publish "$FOLIO" --to https://ingress.interskein.com --dry-run
```

A real (non-dry-run) publish always needs a signing identity: pass `--login` to
run an interactive Sigstore login at the publish boundary, or `--token` for a
token from a prior login. `skein publish` signs the selected folios with your
OIDC identity, and the resulting transparency record is public and permanent.
The verified email from the Sigstore certificate is recorded as the author
identity for that publish.

The collaborator invite flow also signs at the boundary. Redeeming an invite
(`skein station redeem-invite`) binds your Sigstore identity as an author for
that ingress and writes the invite token hash plus your identity to the public
Rekor log. Use the exact invite command from the operator's invite blurb.
