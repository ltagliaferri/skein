# interskein

[![Tests](https://github.com/spiritengine/skein/actions/workflows/test.yml/badge.svg)](https://github.com/spiritengine/skein/actions/workflows/test.yml)
[![Lint](https://github.com/spiritengine/skein/actions/workflows/lint.yml/badge.svg)](https://github.com/spiritengine/skein/actions/workflows/lint.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A knowledge system for agents: local folios, a deliberate publish boundary, and
signed provenance once something crosses it.

`SKEIN` stores local folios — findings, issues, briefs, and summaries — in
per-project sites, then gives you a deliberate boundary for publishing selected
folios to a shared mesh. Local work stays local until you publish it. Signed
publishing uses Sigstore at that boundary so the shared mesh can record a human
identity responsible for a folio.

The public repository is <https://github.com/spiritengine/skein>. The public read
surface is <https://interskein.com>. The public publish ingress is
<https://ingress.interskein.com>.

This is an actively maintained fork of `spiritengine/skein`
(<https://github.com/ltagliaferri/skein>) and may diverge from it.

## Why skein?

- **folios** - Structured knowledge records (findings, issues, briefs,
  summaries), addressed by content hash and stored in per-project sites.
- **sites** - Per-project or per-topic collections of folios. Local until you
  choose to publish them.
- **threads** - Typed edges connecting folios (status, assignment, links) into
  a graph you can query.
- **publish boundary** - Sigstore-signed publishing where local folios cross
  into the shared mesh; a transparency record ties the publish to a real
  identity.
- **mesh** - An HTTP read client for published folios, with local verification
  of every fetch.
- **station** - A self-hostable public-facing server for running your own mesh
  node.

Use skein when you need:
- **Local-first knowledge capture** - Record findings and decisions as you
  work, without committing to sharing them yet
- **A deliberate publish boundary** - Keep drafts and in-progress notes local,
  and choose exactly what crosses into a shared, public record
- **Verifiable provenance** - Every published folio is signed at the boundary
  and recorded in a public transparency log, not just claimed in its own
  fields
- **A queryable knowledge graph** - Connect findings, issues, and briefs with
  typed threads instead of flat notes
- **Durable multi-agent handoffs** - Give agents (and the humans supervising
  them) a shared record that survives past a single session

## Who uses skein?

- **AI agent developers** - Giving agents durable memory for findings and
  handoffs that outlive a single session
- **Security researchers** - Recording findings with signed, publicly
  verifiable provenance
- **Teams running an internal knowledge mesh** - Publishing vetted folios from
  many local workbenches to one shared, verifiable station
- **Anyone wanting a git-like local-first workflow** - for structured notes
  instead of prose docs

## Installation

The distribution is named `interskein`. Install it as a tool, so the CLI and
the API service share one isolated environment:

```bash
uv tool install interskein
```

`pipx install interskein` and `pip install interskein` work the same way; `uv
tool` is the path this project tests.

Trusted collaborator onboarding is a separate, stricter route: use the
`/onboarding` URL in the operator's invitation, which provides wheel-only,
fully hashed requirements plus direct Sigstore signatures over the raw
requirements and collaboration primer. Verify those files against the expected
operator identity before installing. Use that route when an operator invited
you to publish to their mesh; use the plain install above for a local
workbench.

The distribution installs three console scripts, and there is no `interskein`
command:

- `skein`, the local workbench CLI (sites, folios, publish) — also home to the
  `skein station` subcommand group, which runs and operates a public station.
- `mesh`, the HTTP read client for mesh stations.
- `skein-server`, the local API service.

Check the installed version with `skein --version`.

## Quick Start

```bash
# The skein CLI is a client; every command below needs a running service
skein-server &

# Initialize a project (like `git init`) and confirm the install is sound
skein init --project my-project
skein doctor

# Create a site and post a folio to it
skein site create release-notes "Public release notes"
skein post finding release-notes "CLI package renamed" \
  -d "The public distribution installs as interskein; the installed command is skein."
# Posted finding: finding-20260628-a1b2

# List, read, and search
skein sites
skein find --site release-notes
skein folio finding-20260628-a1b2
skein find "Verified local workflow"

# Preview a publish — verifies and prints without sending anything
skein publish finding-20260628-a1b2 --to https://ingress.interskein.com --dry-run
```

## Modules

- **skein** - Local workbench CLI: sites, folios, threads, publish
- **skein station** - Subcommand group that runs and operates a public station
- **mesh** - HTTP read client for mesh stations, with local verification
- **skein-server** - Local API service every `skein` command talks to

## Common Workflows

### Local Knowledge Capture
- `skein init --project NAME` to create a project
- `skein site create` a site to hold related folios
- `skein post <type> <site> <title>` to record a finding, issue, brief, or
  summary
- `skein find` / `skein folio <id>` to search and read
- `skein threads <id>` to see how a folio connects to others
- `skein update <id> <status>` / `skein close <id>` to move it along

### Running a Public Station
- `skein station serve --host ... --port ...` serves the read-only web surface
- Set `SKEIN_STATION_NAME` (or write a stationfile) to name the station
- Station data lives in `.skein-station` by default (`--data-dir` /
  `SKEIN_STATION_DATA_DIR` to move it)

### Reading a Remote Mesh
- `mesh describe --from <url>` to introspect a station
- `mesh search <query> --from <url>` to search it
- `mesh fetch` to resolve an address, verify the returned folio locally, and
  fail non-zero on a verification failure

### Publishing With Signed Provenance
- `skein publish <folio> --to <url> --dry-run` to preview
- `skein publish --site <name> --to <url> --login` to publish a whole site,
  signing with an interactive Sigstore login
- `--token` reuses a prior login; `--slug` sets a public name that differs
  from the local site id

## Detailed Usage

### Local Workbench

Read the built-in quick start at any time — it ships inside the package:

```bash
skein info quickstart
```

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

### Running The Service

Every workbench command talks to a local API service on `127.0.0.1:8001`, so
one has to be running:

```bash
skein-server
```

That runs in the foreground. To keep it running, hand it to whatever
supervises processes on your machine — `skein` deliberately does not
supervise it itself. On Linux with systemd, `skein-server` prints a ready user
unit for this install (its ExecStart already resolved to the installed path,
since a systemd user unit's PATH does not reliably include `~/.local/bin`):

```bash
mkdir -p ~/.config/systemd/user
skein-server --print-unit > ~/.config/systemd/user/skein.service
systemctl --user enable --now skein
systemctl --user status skein          # journalctl --user -u skein -f for logs
```

From a checkout, `make install-service` does the same. Run `loginctl
enable-linger` if you want the service up when you are not logged in.

On macOS, `skein-server` prints a launchd user agent instead:

```bash
mkdir -p ~/Library/LaunchAgents
skein-server --print-plist > ~/Library/LaunchAgents/net.interskein.skein-server.plist
launchctl load -w ~/Library/LaunchAgents/net.interskein.skein-server.plist
```

(The plist's content is pinned by this repo's tests, but launchd itself only
exists on macOS — the load path is exercised there, not here.) On a system
with neither systemd nor launchd, run `skein-server` under whatever supervises
processes there, or in a terminal.

Then confirm the install is sound:

```bash
skein doctor
```

`skein doctor` checks the install, the SKEIN home, the project registry, the
service, whether the CLI and service report the same version, the packaged
documentation, and the current project. It exits non-zero when something is
actually broken, so it works in a script. Run it first whenever a `skein`
command fails in a way you do not recognize.

Data lives under `~/.skein` (override with `SKEIN_HOME`), never in the
directory the service was started from. To move where the service binds,
write the address into `<SKEIN_HOME>/server.json` (`{"host": ..., "port":
...}`) — the one source both `skein-server` and the CLI's URL resolution
read, so both ends move together under any supervisor. `SKEIN_HOST` /
`SKEIN_PORT` do the same only when the service and the CLI share a shell
environment — a supervisor's environment block (systemd `Environment=`,
launchd `EnvironmentVariables`) reaches the service alone, which is why the
packaged units point at `server.json` instead. `SKEIN_URL` points the CLI
somewhere else entirely (a remote service, a second instance). `skein doctor`
names which source its URL came from and reports when nothing is answering
there.

After upgrading the package, restart the service. Otherwise the old one keeps
serving and `skein doctor` reports the version mismatch.

### Running A Station

`skein station` runs the public-facing servers, and the operator ceremonies a
signed station needs to boot. Station data lives in `.skein-station` by
default; point elsewhere with `--data-dir` or `SKEIN_STATION_DATA_DIR`.

Serve the local read-only web surface. `SKEIN_STATION_NAME` sets the
station's display name until a stationfile exists (see
`docs/STATION_THEMING.md`):

```bash
export SKEIN_STATION_NAME=my-station
skein station serve --host 127.0.0.1 --port 9001
```

### Reading The Mesh

`mesh` reads a station over HTTP. Display commands are convenient for
browsing. `mesh fetch` is the strict path: it resolves an address, verifies
the returned folio locally, and exits non-zero on verification failures.

Describe a station (point `--from` at any mesh station):

```bash
mesh describe --from https://interskein.com
```

With no `--from`, `mesh` targets a local station at `http://127.0.0.1:9001`
(the one `skein station serve --port 9001` brings up), so the bare form below
only works while that local server is running:

```bash
mesh describe
```

Search a station:

```bash
mesh search release --from https://interskein.com
```

Use `mesh fetch` when you have a concrete folio address and need local
verification of the returned envelope.

### Publish Boundary

Publishing is separate from local work. A local folio is only a local record
until you send it to an ingress. The ingress verifies content hashes before
storing the batch.

Preview a publish without sending anything:

```bash
skein publish "$FOLIO" --to https://ingress.interskein.com --dry-run
```

Publish a workbench site as a named public station site. With no positional
refs, every current non-site folio head in `gnomon` is declared as a member;
pass refs to publish an exact subset. The preview shows the stable site
anchor, each `within` membership, and the `/site/gnomon` slug claim without
writing local state:

```bash
skein publish --site gnomon --to https://ingress.interskein.com --dry-run
skein publish --site gnomon --to https://ingress.interskein.com --login
```

Use `--slug public-name` when the public slug should differ from the local
workbench site id. Public slugs are 1–32 lowercase letters, digits, or
interior hyphens.

A real (non-dry-run) publish always needs a signing identity: pass `--login`
to run an interactive Sigstore login at the publish boundary, or `--token`
for a token from a prior login. `skein publish` signs the selected folios with
your OIDC identity, and the resulting transparency record is public and
permanent. The verified email from the Sigstore certificate is recorded as the
identity that vouched for that publish; a folio's `created_by` field remains
an unverified content claim.

The collaborator invite flow also signs at the boundary. Redeeming an invite
(`skein station redeem-invite`) binds your Sigstore identity as an author for
that ingress and writes the invite token hash plus your identity to the
public Rekor log. Use the exact invite command from the operator's invite
blurb.

## Requirements

- Python 3.10+
- A running `skein-server` for every `skein` CLI command

## License

MIT
