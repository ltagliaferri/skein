# Public ingress operations — interskein.com write surface

How the operator stands up, runs, and rolls back the public signed-publish
ingress, and how a collaborator is authorized to write.

This is the **write** counterpart to the read surface. The read app
(`interskein.com` → `127.0.0.1:9001`) serves content; the ingress
(`ingress.interskein.com` → `127.0.0.1:9101`) receives signed publishes.

## The security model (why public exposure is safe)

The protection is the crypto + binding gate **inside the app**, never network
obscurity:

- Under `SKEIN_NEXT_REQUIRE_SIGNED=1`, a publish is accepted only if it carries
  a manifest that (a) verifies via Sigstore and (b) whose signer's
  `(issuer, subject)` is in the operator's allow-list and not revoked.
- An unsigned / no-manifest publish is rejected **cheaply**, before any crypto.
- Content is attributed to the real verified signer; revocation is live (the
  binding is recomputed per read, never cached stale).

So the write surface is safe to expose: only bound, signed identities can write.
nginx adds the DoS perimeter (body cap, rate limit, timeouts) on top.

The ingress stays bound to `127.0.0.1:9101`. Docker bypasses UFW via iptables,
so a bare host port would be public regardless of the firewall — nginx is the
**only** public listener, mirroring the read vhost's `127.0.0.1` pin.

## One-time setup

### 1. DNS

`interskein.com` DNS is hosted at DigitalOcean (`ns1/2/3.digitalocean.com`).
Add an A record for the ingress subdomain pointing at the droplet:

```bash
# doctl (re-auth first if the token expired: doctl auth init)
doctl compute domain records create interskein.com \
  --record-type A --record-name ingress --record-data 45.55.249.33 --record-ttl 3600

# verify it resolves before issuing a cert
dig +short ingress.interskein.com A    # -> 45.55.249.33
```

(Or add it in the DO web console: Networking → Domains → interskein.com →
`ingress` A → `45.55.249.33`.)

### 2. TLS certificate

Issue the cert with certbot in `certonly` mode (gets the cert without rewriting
the vhost, so the hand-tuned limits below stay intact):

```bash
certbot certonly --nginx -d ingress.interskein.com
# cert lands at /etc/letsencrypt/live/ingress.interskein.com/
```

### 3. nginx vhost

The vhost is `deploy/nginx/ingress.interskein.com.conf` in the skein repo. It has
two parts:

- The `limit_req_zone` line is **http{}-context** — it must live in a
  `conf.d/*.conf` file (a `server{}` block can't hold it). Either split it out to
  `/etc/nginx/conf.d/interskein-ingress-zone.conf`, or confirm the whole file is
  included at http scope.
- The `server{}` blocks go in `/etc/nginx/sites-available/ingress.interskein.com`
  and are symlinked into `sites-enabled/`.

```bash
# copy the vhost (adjust the split per your include layout)
scp deploy/nginx/ingress.interskein.com.conf \
    root@45.55.249.33:/etc/nginx/sites-available/ingress.interskein.com
ssh root@45.55.249.33 'ln -sf /etc/nginx/sites-available/ingress.interskein.com \
    /etc/nginx/sites-enabled/ && nginx -t && systemctl reload nginx'
```

The DoS perimeter the vhost enforces:

- `client_max_body_size 1m` — **must match** the app's `MAX_BATCH_BYTES`
  (`skein_next/ingress.py`). Bump both together if a legit publish needs more.
- `limit_req` 10 r/m per IP, burst 5 (429 over the limit).
- `client_header_timeout` / `client_body_timeout` / `send_timeout` 10s — slow-loris.
- Only `POST /publish/v0/folios` is proxied; everything else 404s.

### 4. The ingress service (require_signed ON)

The ingress runs as a compose service sharing the served corpus read-write
(`/opt/interskein/compose.yaml` on the droplet), bound `127.0.0.1:9101`, with
`SKEIN_NEXT_REQUIRE_SIGNED=1`.

**`SKEIN_NEXT_ORIGIN` (required for `/invite/redeem`).** The redeem ceremony binds
the collaborator's Sigstore proof to this station's canonical origin. **It must be
the public host that actually serves `/invite/redeem`** — i.e. the same value the
collaborator passes as `--to` — because that one value is both the POST endpoint
and the signed-origin the station reconstructs. Per the vhost (`server_name
ingress.interskein.com`) the served write host is `ingress.interskein.com`, so:

```yaml
  environment:
    - SKEIN_NEXT_DATA_DIR=/data
    - SKEIN_NEXT_REQUIRE_SIGNED=1
    - SKEIN_NEXT_ORIGIN=https://ingress.interskein.com   # NEW — redeem token-binding
```

The station canonicalizes this value the same way the client canonicalizes `--to`
(lowercase scheme+host, drop default ports and trailing slash), so a stray trailing
slash is tolerated. But the **host** must match where the route is served: if you
front the write surface on the apex instead, set this to the apex and point
collaborators there. **Confirm against the LIVE endpoint** (a real
`redeem-invite ... --login` reaches the route and binds) in the hardening pass —
a host mismatch fails closed (SIGNATURE_MISMATCH / unreachable), never silently.

If it is unset, `/publish` is unaffected but `/invite/redeem` refuses to operate
(returns 503 "redeem is not configured") — it cannot reconstruct a token-bound
challenge without an authoritative origin. Redeem works under
`require_signed` ON **or** OFF (it is orthogonal to the publish gate); only the
*consequence* of being a bound author differs.

The redeem route also needs its nginx `location = /invite/redeem` (its own tighter
`redeem_zone` and a 64 KiB body cap) — both are in
`deploy/nginx/ingress.interskein.com.conf`; redeploy the vhost (step 3) when adding
redeem.

**Boot invariant:** under `require_signed` the ingress refuses to boot unless
exactly **one** active operator exists. Bootstrap the operator before first boot:

```bash
interskein --data-dir /data account init-operator \
  --issuer https://accounts.google.com --subject patricksmyth01@gmail.com
```

## Authorizing a collaborator

There are two paths. **Invite (preferred)** is agent-mediated and self-service:
the operator mints a one-time token, the collaborator's agent redeems it, and the
binding happens automatically — no subject relay. **Manual `account add`** is the
fallback when you already know the identity.

### Invite flow (preferred)

```bash
# 1. mint a one-time invite (token shown ONCE; only its hash is stored)
interskein --data-dir /data account invite mint --role author \
  --expires 7d --note "Alice" --origin https://ingress.interskein.com
#    -> prints the token + a ready-to-send blurb. Send it to the collaborator
#       OUT OF BAND (you vouch for the human, not the channel).

# 2. the collaborator's agent redeems it (their side):
#    interskein redeem-invite <token> --to https://ingress.interskein.com --login
#    -> a token-bound Sigstore ceremony auto-binds them as an author.

# 3. see who redeemed (operator-visible — a hostile bind is detectable here):
interskein --data-dir /data account invite list
#    redeemed author by alice@example.com at 2026-... "Alice" <hash>

# revoke an OUTSTANDING invite before it is redeemed (by token, or --hash <prefix>)
interskein --data-dir /data account invite revoke --hash <hash-prefix>
```

If a redemption binds an identity you did not expect, revoke the *binding* with
`account revoke` (below) — a revoked identity cannot self-readmit via a later
redeem.

### Manual fallback (`account add`)

```bash
# the collaborator runs this to read back their SUBJECT (no Rekor entry):
interskein whoami            # -> issuer https://oauth2.sigstore.dev/auth   (the broker
                             #       TOKEN issuer — NOT the cert issuer; see note below)
                             #    subject their-email@example.com

# add an author (vouched for by the active operator). The --issuer is the CERT
# issuer (the federated upstream provider), e.g. https://accounts.google.com for a
# Google login — NOT the broker value whoami prints. See the note below.
interskein --data-dir /data account add --role author \
  --issuer https://accounts.google.com --subject their-email@example.com

# list current bindings (one per line: <role> <issuer>/<subject>)
interskein --data-dir /data account list

# revoke a binding (revocation is not deletion; takes effect live, next read/publish)
interskein --data-dir /data account revoke \
  --issuer https://accounts.google.com --subject their-email@example.com
```

`interskein whoami` closes the old SUBJECT-discovery gap: it prints the verified
subject (the email the cert SAN will carry), read off the OIDC identity token
without creating a Rekor entry (finding-20260615-61z7). Its **issuer** value is the
token/broker issuer, which differs from the cert issuer used by the binding — see
the note below; take the binding `--issuer` from there, not from `whoami`.

> **Cert issuer vs token issuer (empirically confirmed 2026-06-20):**
> A human Sigstore login goes through the Dex broker
> (`https://oauth2.sigstore.dev/auth`) for the OIDC ceremony, but Fulcio mints a
> cert whose issuer extension carries the **federated upstream provider** — for a
> Google login that is `https://accounts.google.com`.  `can_write()` keys on the
> cert issuer, not the token issuer.  `interskein whoami` prints the **token**
> issuer (the broker); do not bootstrap a binding from that value.  To discover the
> real cert issuer, decode an actual stored cert or signed manifest, or use the
> invite/redeem flow (it auto-binds the cert identity, no manual issuer needed).

## Verifying the deployment

```bash
SUB=https://ingress.interskein.com/publish/v0/folios

# unsigned publish is rejected cheaply (nothing stored)
curl -s -X POST "$SUB" -H 'Content-Type: application/json' \
  -d '{"protocol":"skein.publish/v0","folios":[],"threads":[]}' ; echo

# wrong protocol -> 400
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$SUB" \
  -H 'Content-Type: application/json' -d '{"protocol":"bogus"}'        # 400

# oversized body -> 413 (before parse)
head -c 1100000 /dev/zero | tr '\0' 'x' > /tmp/big.json
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$SUB" \
  -H 'Content-Type: application/json' --data-binary @/tmp/big.json     # 413

# rate limit -> some 429s once burst is exhausted
for i in $(seq 1 12); do
  curl -s -o /dev/null -w '%{http_code} ' -X POST "$SUB" \
    -H 'Content-Type: application/json' -d '{"protocol":"bogus"}'
done; echo

# non-POST method on the route -> 405
curl -s -o /dev/null -w '%{http_code}\n' "$SUB"                        # 405
```

Redeem-route probes (no valid token needed — these exercise the hardening shell):

```bash
RED=https://ingress.interskein.com/invite/redeem

# unknown token -> 409 (rejected cheaply, before any crypto)
curl -s -w '\n%{http_code}\n' -X POST "$RED" -H 'Content-Type: application/json' \
  -d '{"token":"definitely-not-a-real-token","proof":{}}'             # 409 unknown_token

# malformed JSON -> 400 ; missing token -> 400
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$RED" \
  -H 'Content-Type: application/json' -d '{not json'                   # 400
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$RED" \
  -H 'Content-Type: application/json' -d '{"proof":{}}'               # 400

# oversized body (>64 KiB) -> 413 before parse
head -c 70000 /dev/zero | tr '\0' 'x' > /tmp/big-redeem
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$RED" \
  -H 'Content-Type: application/json' --data-binary @/tmp/big-redeem   # 413

# tighter rate limit than publish (5 r/m) -> 429s sooner under a burst
for i in $(seq 1 8); do
  curl -s -o /dev/null -w '%{http_code} ' -X POST "$RED" \
    -H 'Content-Type: application/json' -d '{"token":"x","proof":{}}'
done; echo

# if SKEIN_NEXT_ORIGIN is unset on the ingress -> 503 "redeem is not configured"
```

The full redeem round-trip (a real `interskein redeem-invite ... --login`) needs an
interactive OIDC ceremony — see the collaborator onboarding doc
(`docs/COLLABORATOR_ONBOARDING.md`).

The full signed round-trip (a real `interskein publish --sign --login`) needs an
interactive OIDC ceremony — see the collaborator publish doc.

## Schema migration (invite/redeem tables)

The invite/redeem bundle adds two tables to the corpus — `invites` and
`invite_events`. The migration is **purely additive and automatic**: the store's
`CREATE TABLE IF NOT EXISTS` DDL runs on every read_write open (the ingress open),
so the first time the new ingress image opens the served corpus the two tables
materialize. There is **no `ALTER`, no data backfill, no downtime step**.
Validated on a copy of a pre-migration corpus in
`skein_next/tests/test_invite_redeem_migration.py`:

- Opening read_write materializes both tables (empty); **every pre-existing
  folio/thread/binding row is byte-identical** afterward.
- The migration is **idempotent** (a second open changes nothing).
- The **read surface (`:ro` mount) is unaffected** — the read path never queries
  the invite tables (only `store.py`/`cli.py` do), so their absence-then-presence
  is invisible to it. No read-app restart is needed for the migration.
- Under `require_signed=OFF` a publish is **byte-identical** whether or not the
  tables exist (they are write-surface state `/publish` never reads).

Deploy steps:

```bash
# 1. predeploy backup (same convention as the existing full-restore backups)
ssh root@45.55.249.33 'cp /srv/interskein/corpus/store.db \
  /srv/interskein/corpus/store.db.bak-predeploy-$(date +%Y%m%d-%H%M%S)'
# 2. roll the ingress image; first read_write open runs the additive DDL.
# 3. confirm the tables materialized (and stayed empty until first mint):
ssh root@45.55.249.33 "sqlite3 /srv/interskein/corpus/store.db \
  \"SELECT name FROM sqlite_master WHERE name IN ('invites','invite_events');\""
#    -> invites / invite_events
```

Migration rollback: the tables are additive and unused by `/publish` and under
`require_signed`, so **leaving them in place is harmless**. A true revert is the
predeploy corpus restore below (which drops them back); the `require_signed=OFF`
config-flip rollback is byte-identical with or without them.

## Rollback

Rollback is a **config flip**, byte-identical to the pre-mesh posture:

- **Disable enforcement / stop accepting writes:** set
  `SKEIN_NEXT_REQUIRE_SIGNED=0` and recreate the ingress (it now accepts
  unsigned — only do this if you also pull the public route), **or** simply take
  the public route down:

  ```bash
  ssh root@45.55.249.33 'rm /etc/nginx/sites-enabled/ingress.interskein.com \
    && nginx -t && systemctl reload nginx'
  ```

  Removing the symlink returns the write surface to dark (loopback-only) without
  touching the read surface or the corpus.
- **Full restore:** the predeploy corpus + compose backups are on the droplet
  (`/srv/interskein/corpus*/store.db.bak-predeploy-*`,
  `/opt/interskein/compose.yaml.bak-*`). See finding-20260615-m6qy.

## Concurrency notes

The ingress is a single process; writes serialize through it. The corpus is on a
shared writable volume in rollback-journal mode (not WAL — the read mount is
`:ro`). The read surface tolerates the ingress writing underneath it (the
read-path missing-table tolerance is read-only-scoped). Validate read-path health
while the ingress is under write load before declaring a deploy good.

**Threadpool sizing vs. the redeem rate limit.** Each `/invite/redeem` does a
multi-second Sigstore `verify_multi` OFF the write lock, but ON an anyio
threadpool worker. The verify provably holds no DB lock (so a slow redeem never
wedges publishes — `skein_next/tests/.../test_verify_multi_holds_no_write_lock`),
but a large burst of concurrent slow verifies can saturate the default ~40-thread
pool and inflate other requests' latency (graceful — no 500s, no lock wedge; it is
pool capacity, not the DB). The `redeem_zone` 5 r/m per-IP cap is the front-line
that keeps real traffic far below this; if you ever raise that cap, raise the
uvicorn/anyio threadpool to match. Not a code fix — an operator tuning knob.
