# Collaborator onboarding — redeem an invite, publish signed

This is the **collaborator** side of agent-mediated onboarding (the operator side
— minting invites, bindings, revocation — lives with the station's deployment
runbooks, not in this repo). You were sent a one-time invite token out
of band. Hand this whole document to your coding agent; it walks the agent through
installing the CLI, redeeming the invite, and publishing — surfacing provenance and
the Rekor-consent stop for you to confirm.

## The flow

1. The operator minted an invite and sent you a token + a short blurb.
2. Your agent installs the verified `skein` CLI (distributed as the `interskein`
   package; see the install section).
3. Your agent appends a fixed primer to your agent file (AGENTS.md / CLAUDE.md /
   .cursorrules) — detect-and-**append**, never clobber.
4. Your agent redeems the invite:

   ```bash
   skein station redeem-invite <token> --to https://ingress.interskein.com --login
   ```

   This runs a Sigstore login (the human-accountability gate) and signs a proof
   that is **cryptographically bound to your token and to this station** — a
   harvested signature for some other purpose cannot bind your identity here. The
   station verifies it, atomically burns the invite, and binds your discovered
   `(issuer, subject)` as an author.

5. You publish; your content renders **SIGNED** under your identity.

## What your agent MUST surface to you before acting

These are non-negotiable confirmation stops — your agent runs downstream of an
out-of-band message it cannot fully trust, so **you** are the check:

- **Provenance.** Where the CLI is being installed from, and the exact version /
  artifact hash (see install section). Confirm it matches the signed install spec.
- **The exact config additions.** The verbatim primer snippet being appended to
  your agent file — diff it against the published known-good snippet. It is a
  fixed, published string, never free-form generated from the invite blurb.
- **Rekor consent.** Redeeming **signs with your identity and writes a record to
  the public Rekor transparency log** — your invite token's hash and your email
  become permanently public. `skein station redeem-invite` prompts you to confirm
  before the ceremony; on a headless box (`--oob`) there is no other
  human-in-the-loop, so this confirmation is the hard stop. Do not pass `--yes`
  unless you have read and accepted this.

## Discovering your identity (optional)

```bash
skein station whoami          # prints: issuer <...> / subject <your-email>
skein station whoami --oob    # SSH/headless code flow
```

`whoami` reads your OIDC identity directly — **no** Rekor entry, **no** cert. It is
the exact `(issuer, subject)` your binding will use, useful if the operator is
adding you manually instead of by invite.

## Redeem outcomes

`redeem-invite` exits 0 on success (`redeemed — bound as author: <you>`). On
failure it prints the typed reason:

- `unknown_token` / `expired` / `revoked_invite` / `already_redeemed` — the token
  is not usable; ask the operator for a fresh invite (or check you copied it whole).
- `revoked_identity` — the operator revoked your identity; it cannot self-readmit.
  Contact the operator.
- `proof_rejected` / `proof_malformed` — the signature didn't verify against this
  token + station; re-run a clean `--login` (don't reuse a stale proof).
- `rate_limited` — too many failed attempts on this token; wait and retry.

A lost network ack is safe: re-running `redeem-invite` with the same identity is
**idempotent** — if you already redeemed, the retry reports success, not an error.

## Install the CLI

Your agent is downstream of an invite packet it cannot fully trust. The website's
rendered `SIGNED` badge is not the bootstrap trust check. The load-bearing files are
the raw requirements and primer plus their direct Sigstore bundles, fetched from
the `/onboarding` URL in the invitation and verified locally before installation.

### Confirm the operator identity

This is the check only the invited human can make. Confirm independently that the
person who invited you uses this certificate identity:

```text
subject patricksmyth01@gmail.com
issuer  https://accounts.google.com
```

Do not take that identity only from the invitation or onboarding page. Any other
identity is a hard stop.

### Download and freshness-check the pack

Download all six files from the invitation's read-surface `/onboarding` URL:

```text
sigstore-pinned.txt
sigstore-pinned.txt.sigstore.json
interskein-pinned.txt
interskein-pinned.txt.sigstore.json
interskein-primer.txt
interskein-primer.txt.sigstore.json
```

Compare the SHA256 digests of the three raw `.txt` files with the values supplied
through the independent Patrick-controlled channel. This rejects an old but
genuinely signed pack as well as modified bytes. The hashes displayed by the same
website are convenient diagnostics, not an independent trust root.

### Install the pinned verifier

The verifier is also a trust root. Install its wheel-only, fully hashed dependency
set from the fixed PyPI index:

```bash
python -m pip install --require-hashes --only-binary=:all: \
  --index-url https://pypi.org/simple -r sigstore-pinned.txt
```

### Verify every raw file, fail closed

Run each command against its adjacent bundle. Every command must exit zero; an
unavailable verifier or any result short of verified is a hard stop.

```bash
python -m sigstore verify identity \
  --cert-identity patricksmyth01@gmail.com \
  --cert-oidc-issuer https://accounts.google.com \
  --bundle sigstore-pinned.txt.sigstore.json \
  --offline sigstore-pinned.txt

python -m sigstore verify identity \
  --cert-identity patricksmyth01@gmail.com \
  --cert-oidc-issuer https://accounts.google.com \
  --bundle interskein-pinned.txt.sigstore.json \
  --offline interskein-pinned.txt

python -m sigstore verify identity \
  --cert-identity patricksmyth01@gmail.com \
  --cert-oidc-issuer https://accounts.google.com \
  --bundle interskein-primer.txt.sigstore.json \
  --offline interskein-primer.txt
```

### Install interskein, wheels only

```bash
python -m pip install --require-hashes --only-binary=:all: \
  --index-url https://pypi.org/simple -r interskein-pinned.txt
```

`--require-hashes` covers the full transitive tree; `--only-binary=:all:` prevents
an sdist build backend from executing. Confirm the installed distribution and
version without resurrecting a nonexistent `interskein` command:

```bash
python -c "from importlib.metadata import version; print(version('interskein'))"
```

The signed install spec names the Git commit for source audit. For the first
release it is audit information, not a wheel-to-source provenance attestation.

Redeeming and publishing talk to the station over HTTPS and need nothing else
running. The *local* workbench (`skein init`, `skein post`, `skein find`) is a
client of a local API service, so start `skein-server` before using it, then:

```bash
skein doctor
```

It exits non-zero if anything about the install is wrong, including a CLI and
service that report different versions.

### Append the primer verbatim

Append the verified `interskein-primer.txt` bytes verbatim to the repository's
agent instruction file (`AGENTS.md`, `CLAUDE.md`, or `.cursorrules`). Detect and
append; never overwrite the file, generate substitute text from the invitation,
or diff against a fresh unverified website render.

The CLI does not try to verify its own artifact during redemption. Once a program
is already executing, it cannot establish that it was not replaced; the
pre-execution hash and signature checks above are the integrity boundary.
