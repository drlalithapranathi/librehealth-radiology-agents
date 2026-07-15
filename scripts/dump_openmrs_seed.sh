#!/usr/bin/env bash
# Capture the running OpenMRS DB as a fast-restore seed (issue #72; see docker-compose.seed.yml).
#
# Run this ONCE against a healthy clean boot. It writes docker/openmrs/seed/openmrs-seed.sql.gz,
# which docker-compose.seed.yml reloads on a fresh volume to skip the ~16 min first boot.
#
# The seed is DB data (not code) and is gitignored: it will carry MIMIC/PHI once the #68 cohort is
# loaded, and the PhysioNet DUA forbids redistribution. Keep it on the access-controlled host only.
set -euo pipefail

MARIADB_CONTAINER="${MARIADB_CONTAINER:-lh-radiology-agents-mariadb-1}"
OUT="${1:-docker/openmrs/seed/openmrs-seed.sql.gz}"

if ! docker ps --format '{{.Names}}' | grep -qx "$MARIADB_CONTAINER"; then
  echo "mariadb container '$MARIADB_CONTAINER' is not running; boot the stack first" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"
echo "dumping openmrs DB from $MARIADB_CONTAINER -> $OUT ..."
# No routines/triggers: OpenMRS uses none, and their DEFINER clauses can trip an initdb restore.
docker exec "$MARIADB_CONTAINER" sh -c \
  'exec mysqldump -uopenmrs -popenmrs --single-transaction --no-tablespaces --skip-add-locks openmrs' \
  | gzip > "$OUT"

echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
echo "restore with: docker compose down -v && docker compose -f docker-compose.yml -f docker-compose.seed.yml up -d"
