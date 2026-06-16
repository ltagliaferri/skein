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

**Boot invariant:** under `require_signed` the ingress refuses to boot unless
exactly **one** active operator exists. Bootstrap the operator before first boot:

```bash
interskein --data-dir /data account init-operator \
  --issuer https://accounts.google.com --subject patricksmyth01@gmail.com
```

## Authorizing a collaborator

A collaborator can write only after the operator binds their verified identity.

```bash
# add an author (vouched for by the active operator)
interskein --data-dir /data account add --role author \
  --issuer https://accounts.google.com --subject their-email@example.com

# list current bindings (one per line: <role> <issuer>/<subject>)
interskein --data-dir /data account list

# revoke (revocation is not deletion; takes effect live, next read/publish)
interskein --data-dir /data account revoke \
  --issuer https://accounts.google.com --subject their-email@example.com
```

The collaborator must communicate their OIDC **issuer** and **subject** to the
operator out-of-band (the operator trusts the person, not a channel). For Google
the issuer is `https://accounts.google.com` and the subject is their email.

> **Onboarding-UX gap (pairing pending):** `interskein login` prints the issuer
> and the token but **not** the subject — the exact value `account add --subject`
> needs. A collaborator currently has to know their subject out of band. Closing
> this (a `whoami`-style helper that prints `(issuer, subject)` after login) is
> the open UX item for the collaborator-facing publish doc. See
> finding-20260615-61z7.

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

The full signed round-trip (a real `interskein publish --sign --login`) needs an
interactive OIDC ceremony — see the collaborator publish doc.

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
