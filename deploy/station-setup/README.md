# Station setup packet — stand up a signed SKEIN station

A turnkey, parameterized recipe for standing up a public signed station (a read
surface + a signed-publish ingress) from the `skein station` build on a droplet.
Fill in the vars once (`vars.example`), then work the checklist top to bottom. This
distills the interskein.com cutover (2026-07-11) so the next station — darkive, or any
future one — is not re-derived. Both stations run the SAME image; only env, corpus,
domains, and ports differ.

The full prose companion is `~/production/digitalocean/interskein/PUBLIC_INGRESS_OPERATIONS.md`
+ `LIVE_CUTOVER_RUNBOOK.md`. This packet is the reusable skeleton; those are the
interskein-specific record.

## Files in this packet
- `README.md` — this runbook + checklist.
- `compose.template.yaml` — the two-service (read + ingress) station compose, with
  `{{PLACEHOLDER}}` vars.
- `nginx-read.conf.template` — the read vhost.
- `../nginx/ingress.interskein.com.conf` — use as the INGRESS vhost template (copy it,
  rename, swap `server_name` + cert paths + upstream port; it already carries the body
  caps, rate limits, and the POST-only `/publish/v0/folios` + `/invite/redeem` gate).
- `vars.example` — the variables to fill in.

## Variables (see vars.example)
`STATION_NAME` (page title + bootstrap name) · `READ_DOMAIN` (e.g. darkive.org) ·
`INGRESS_DOMAIN` (e.g. ingress.darkive.org) · `READ_PORT` / `INGRESS_PORT` (loopback,
distinct per station co-hosted on one box) · `IMAGE_TAG` (e.g. interskein:station-YYYYMMDD)
· `DATA_DIR` (host path, e.g. /srv/<name>/corpus) · `OPERATOR_ISSUER` / `OPERATOR_SUBJECT`
(the trust anchor's verified OIDC identity) · `DROPLET` (root ssh target).

## Golden rule: validate the WHOLE stand-up locally FIRST
Before touching the droplet, stand the station up locally end-to-end — fresh data dir,
`init-operator`, boot read + ingress under `require_signed`, and a signed round-trip that
renders SIGNED — with the SKEIN_STATION_* env only and ZERO deprecation warnings. Only
then ship. This is the "run on a copy first" discipline applied to the deploy; it caught
every interskein cutover surprise before it was live.

## Security prerequisite (before real collaborators)
A station that will bind non-operator authors MUST carry the supersedes-edge target
authorization fix in its deployed image FIRST. Without it a bound collaborator can publish
`supersedes(their_folio -> another_site_head)` and redirect that site's slug. Fix: admit a
supersedes edge only if the manifest signer == the attributed signer of the target `to_id`
(constituent_attribution), or the signer is the operator; reject a supersedes to an unheld,
non-signer-owned target. A specs-only / single-operator station (e.g. interskein) is not
exposed and can defer it; a collaborator station (e.g. darkive) CANNOT. See
`finding-20260710-lx37`.

## Checklist

### 0. Build + ship the image (from the dev box, on the deployed commit)
```
docker build -t {{IMAGE_TAG}} .
docker save {{IMAGE_TAG}} | gzip | ssh root@{{DROPLET}} 'gunzip | docker load'
```
The droplet NEVER builds (it runs the shipped pinned image; the on-box checkout is an
archive, not a clone). Retain the prior image tag for rollback.

### 1. Data dir + operator (require_signed needs exactly ONE active operator to boot)
The box has no host CLI; run ceremonies through the image against the data dir. The dir
must be writable by the image user (uid 10001):
```
ssh root@{{DROPLET}} 'mkdir -p {{DATA_DIR}} && chown 10001:10001 {{DATA_DIR}}'
ssh root@{{DROPLET}} 'docker run --rm -v {{DATA_DIR}}:/data -e SKEIN_STATION_DATA_DIR=/data \
  {{IMAGE_TAG}} skein station account init-operator \
  --issuer {{OPERATOR_ISSUER}} --subject {{OPERATOR_SUBJECT}}'
```
This creates `{{DATA_DIR}}/skein.db` (the station db) with one operator binding. init-operator
only RECORDS the binding (no ceremony); the actual Sigstore signing happens when the operator
publishes. Any active binding — operator or author — authorizes publishing.

### 2. compose
Fill `compose.template.yaml` and place it at `/opt/{{STATION_NAME}}/compose.yaml` on the box.
Read service uses the image CMD (`skein station … serve`); the ingress service overrides
`command:` with `skein station … ingress`. Pinned image, NO `build:` stanza.

### 3. DNS (ALL records must resolve to the box before the step-4 certs)
Create the read apex + www + ingress A records. (If the read domain already points
at the box — e.g. a pre-existing apex — create only the ingress record.)
```
for name in @ www {{INGRESS_SUBDOMAIN}}; do
  doctl compute domain records create {{READ_DOMAIN_ZONE}} --record-type A \
    --record-name "$name" --record-data <droplet-ip> --record-ttl 3600
done
dig +short {{READ_DOMAIN}} A; dig +short {{INGRESS_DOMAIN}} A   # confirm before issuing certs
```

### 4. TLS (certonly keeps hand-tuned vhosts intact)
```
certbot certonly --nginx -d {{READ_DOMAIN}} -d www.{{READ_DOMAIN}}
certbot certonly --nginx -d {{INGRESS_DOMAIN}}
```

### 5. nginx
- Read vhost: `nginx-read.conf.template` → `/etc/nginx/sites-available/{{STATION_NAME}}` →
  symlink into `sites-enabled/`.
- Ingress vhost: copy `../nginx/ingress.interskein.com.conf`, swap `server_name`, cert paths,
  and the `proxy_pass` upstream port to `{{INGRESS_PORT}}`. It proxies ONLY
  `POST /publish/v0/folios` + `POST /invite/redeem`; keep the body caps (must match the app's
  `MAX_BATCH_BYTES` / `REDEEM_MAX_BYTES` in `skein/ingress.py`) and rate limits.
- `limit_req_zone` is http-context — it lives in a `conf.d/*.conf`, not a `server{}`.
- CO-HOSTING CAVEAT: on a shared `[::]:443` socket, exactly ONE vhost carries `ipv6only=on`;
  the others must omit it (duplicate-listen-options error otherwise).
```
nginx -t && systemctl reload nginx
```

### 6. Bring up + verify (no signing needed for most of this)
```
cd /opt/{{STATION_NAME}} && STATION_CORPUS={{DATA_DIR}} docker compose up -d
```
Verify: both containers healthy; boot logs show ZERO `FutureWarning`/`SKEIN_NEXT` lines;
`require_signed` ingress booted (operator present); `curl https://{{READ_DOMAIN}}/.well-known/skein.json`
shows `profile: skein.folio.canon/v1`; `GET https://{{INGRESS_DOMAIN}}/publish/v0/folios` is
405/403 (POST-only route exists); an UNSIGNED publish is rejected.

### 7. Signed round-trip (needs the operator at a workstation)
From a workbench, publish a folio and confirm it renders SIGNED:
```
skein publish <folio-id> --to https://{{INGRESS_DOMAIN}} --login
# -> https://{{READ_DOMAIN}}/folio/<content-hash>  renders SIGNED, bound to the operator
```
Publishing runs over 443 (works regardless of the SSH VPN state).

### 8. Onboarding (collaborator stations) — AFTER the security prereq is deployed
```
# operator mints ON THE BOX — no host CLI, so via the image (like init-operator):
docker run --rm -v {{DATA_DIR}}:/data -e SKEIN_STATION_DATA_DIR=/data {{IMAGE_TAG}} \
  skein station invite mint --role author                 # token shown ONCE
# collaborator, from their workstation:
#   skein station redeem-invite <token> --to https://{{INGRESS_DOMAIN}} --login
```

## Rollback
`docker compose down`; restore the prior compose + image; move the fresh `skein.db` aside and
restore the prior corpus. The retained image + set-aside corpus make the read surface roll back
freely; a signed station's ingress is forward-only once the first live write lands.

## Gotchas (consolidated)
- The corpus DB file is `skein.db` (the `skein_next` build used `store.db`).
- `require_signed` refuses to boot without exactly one active operator — init it first (step 1).
- `SKEIN_STATION_ORIGIN` MUST equal the public ingress host collaborators pass as `--to` — it's
  both the POST endpoint and the signed-origin the redeem ceremony reconstructs. A host mismatch
  fails closed, never silently. Unset → `/publish` works but `/invite/redeem` returns 503.
- A station MUST be named: set `SKEIN_STATION_NAME` (or a stationfile `name`) or `create_app` refuses.
- Env keys are `SKEIN_STATION_*` ONLY. The `SKEIN_NEXT_*` aliases were deleted at Stage 8 and no
  longer resolve.
- A CODE change needs an image ship + `--force-recreate`; only CONTENT flows through the ingress.
- Deploy SSH note (interskein droplet): port 22 is blocked through the `sbccvpn` VPN — SSH works
  only with it DOWN; 443/publish works either way.
