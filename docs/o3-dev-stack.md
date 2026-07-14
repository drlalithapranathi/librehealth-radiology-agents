# Running the OpenMRS o3 backend (dev stack)

The `openmrs` service in `docker-compose.yml` is the LibreHealth Radiology o3
backend (the OpenMRS reference application with the radiology omod baked in).
A few things about bringing it up locally.

## First boot is slow, and that is expected

A clean first boot runs Liquibase (~1800 changesets), imports the concept
dictionary (~4200 concepts), and starts every module. That takes:

- **~10 minutes on native arm64** (Apple Silicon).
- **~18 to 23 minutes under emulation** (an amd64 image on an arm64 host).

The `:o3` image is published multi-arch (amd64 + arm64), so `docker compose up`
pulls the native variant per host and avoids the emulation tax. If a stale
amd64 `:o3` is already cached locally, force the arm64 variant once:

```
docker pull --platform linux/arm64 registry.gitlab.com/librehealth/radiology/lh-radiology/o3:o3
```

`docker compose pull` will NOT replace a locally cached image, because the
service uses `pull_policy: missing`.

## Readiness: 302 means "still booting", 200 means "ready"

For the entire boot, OpenMRS 302-redirects every request to
`/openmrs/initialsetup`. That is normal and clears only when startup fully
completes. A plain `curl -f` treats that 302 as success, so the compose
healthcheck instead gates on the session endpoint returning **200**. That makes
`depends_on: service_healthy` wait for a usable server, not just a listening
one. To check by hand:

```
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/openmrs/ws/rest/v1/session
# 302 -> still booting;  200 -> ready
```

## Clean boot is the supported path; do not reuse the volume across restarts

Bring the stack up from empty volumes:

```
docker compose down -v
docker compose up -d mariadb openmrs
```

Recreating the `openmrs` container against an **existing** `mariadb-data` volume
is not supported. The initializer and OpenConceptLab modules re-load reference
data, hit `already exists` validation errors, abort module startup, and leave
the server stuck at `/initialsetup`. If a stack gets wedged that way, run
`docker compose down -v` and boot clean.

Making the module reference-data load idempotent so a restart does not collide
would remove this constraint. That is an o3-image change (sibling `lh-radiology`
repo) and is tracked separately.
