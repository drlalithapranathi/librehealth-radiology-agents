# Hosted-showcase TLS overlay (#75, repo half)

One command turns the dev stack into the hosted posture:

```bash
SIGNOFF_OVERRIDE_TOKEN=<real> A2A_CALLBACK_TOKEN=<real> \
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d
```

`docker-compose.tls.yml` adds a digest-pinned Caddy in front of everything and rebinds every
other published port to `127.0.0.1`. Off-box, the host exposes **80/443 and nothing else**;
host-side ops tooling (MIMIC ETL → Orthanc, report seeder → fhir2, temporal-ui/jaeger via SSH
tunnel) keeps its localhost ports unchanged.

## The three routes (docker/caddy/Caddyfile)

| Route | Upstream | Auth |
|---|---|---|
| `/openmrs/*` | `openmrs:8080` (RIS UI + fhir2) | OpenMRS's own accounts — radiologists sign under their own identity (#75 item 4); the agents' fhir2 calls carry `FHIR2_BASIC_*` against fhir2 itself. Deliberately **not** behind the proxy login (stacking would break both). |
| `/ingress/*` (path stripped) | `orchestrator:8090` | The #57 override token, app-side. On this route so `SIGNOFF_OVERRIDE_URL` is tappable from a paged clinician's phone (#75 item 5). |
| everything else | `ohif:80` | Proxy-level `basic_auth` (#75 item 3 accepts proxy-level for the demo). OHIF's nginx already fans out same-origin to `/dicom-web` (Orthanc) and `/reading-api` (worklist-api), so **one login guards the viewer, the images, and the reading worklist**. |

## Knobs (all env, all defaulted for a localhost rehearsal)

- `DEMO_DOMAIN` (default `localhost`) — the public name. On a real FQDN also set `DEMO_TLS` to
  an operator email and Caddy switches from its internal CA to ACME/Let's Encrypt automatically.
- `DEMO_PROXY_USER` / `DEMO_PROXY_PASS_HASH` — the viewer login. Rehearsal default is
  `demo` / `change-me`; generate a real hash with
  `docker run --rm caddy:2.8 caddy hash-password -p '<password>'` and pass it (single-quote it —
  bcrypt hashes contain `$`).
- `SIGNOFF_OVERRIDE_TOKEN`, `A2A_CALLBACK_TOKEN` — **required**; the overlay refuses to start on
  the dev defaults the base compose ships.

## How the agents ride https in-cluster (#30's transport condition)

The orchestrator (reads, the RIS poller, and the #26 pre-sign write), communications, and the
EHR assistant get `FHIR2_BASE_URL=https://caddy/openmrs/ws/fhir2/R4` and their plaintext opt-ins
**withdrawn** (set to empty). Trust for Caddy's internal CA is one env var: `SSL_CERT_FILE`
pointed at the CA root on the shared `caddy-data` volume — Python's `ssl` honours it, no image
change. The Caddyfile serves `https://caddy` alongside `https://$DEMO_DOMAIN` exactly so the
in-cluster hostname has a valid cert — as its **own site block pinned to `tls internal`**, never
sharing the public block's tls directive. `DEMO_TLS` only steers the public name: an ACME CA
cannot validate the in-cluster-only name `caddy` (rejectedIdentifier), and `SSL_CERT_FILE`
trusts the internal CA anyway, so `caddy` stays on the internal issuer regardless of `DEMO_TLS`.

## Live-verified (2026-07-19, against the running dev stack)

- Merged-config port audit: the ONLY all-interfaces bindings are caddy's 80/443; every other
  published port is `127.0.0.1`.
- Edge, driven live: `/` without creds → 401; with the rehearsal login → the OHIF viewer;
  `/openmrs/ws/fhir2/R4/metadata` with fhir2 auth → 200; `/ingress/*` → routed to the
  orchestrator; plain http → 308 to https.
- In-cluster, driven live: the real `Fhir2Client` in a container on the compose network, with
  `FHIR2_BASE_URL=https://caddy/...` + `SSL_CERT_FILE` and **no insecure opt-in set**, passed the
  #30 write-transport guard and fetched live Patient data through the proxy.
- The missing-secrets refusal fires: `docker compose config` errors without the two tokens.

## Still open on #75 after this overlay (host-side or follow-up)

1. **Least-privilege fhir2 service account** (#30's remaining condition) — an OpenMRS accounts
   task; the env swap here is then one value.
2. **Radiologist OpenMRS accounts** seeded on the demo host.
3. **In-cluster Orthanc auth** — after this overlay Orthanc is loopback+cluster-only, but its
   anonymous mode is unchanged; turning `AuthenticationEnabled` on also means teaching the OHIF
   nginx hop and the ingress reader to send credentials. Follow-up, not a demo blocker.
4. **Host**: the amd64 VM, DNS for `DEMO_DOMAIN`, DUA-compliant access control (#75 item 6).

## Operational notes

- Caddy mints its CA on first start; agents that race it log a transient cert error and their
  pollers retry — self-heals within seconds.
- `SIGNOFF_OVERRIDE_URL` becomes `https://$DEMO_DOMAIN/ingress/signoff/{workflowId}/override`;
  the proxy strips `/ingress`, so the orchestrator serves the same paths it always did.
- Never `docker compose down` the o3 stack (see docs/o3-dev-stack.md); the overlay changes no
  stateful service and adds none.
