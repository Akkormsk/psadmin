#!/usr/bin/env bash
# Test twin control, run from the twin's code dir (~/psadmin-test/deploy/vds).
#   twin.sh up          build and start the twin from ~/psadmin-test, migrate its DB copy
#   twin.sh refresh-db  replace the twin's DB with a fresh copy of the prod DB
set -euo pipefail
cd "$(dirname "$0")"
twin() { docker compose -f compose.test.yml "$@"; }
prod() { docker compose -f "$HOME/psadmin/deploy/vds/compose.yml" "$@"; }

wait_healthy() {
  local status=starting
  for _ in $(seq 1 30); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$(twin ps -q test-web)")
    [ "$status" = healthy ] && { echo "twin web: healthy"; return 0; }
    sleep 3
  done
  echo "twin web: $status" >&2
  twin logs --tail 50 test-web >&2
  return 1
}

case "${1:-}" in
  up)
    twin build test-web
    twin up -d test-db
    twin run --rm -T test-web python manage.py migrate --noinput
    twin up -d
    wait_healthy
    ;;
  refresh-db)
    twin stop test-web
    twin up -d --wait test-db
    twin exec -T test-db dropdb -U psadmin --if-exists psadmin
    twin exec -T test-db createdb -U psadmin psadmin
    prod exec -T db pg_dump -U psadmin -d psadmin -Fc | twin exec -T test-db pg_restore -U psadmin -d psadmin --no-owner --exit-on-error
    twin run --rm -T test-web python manage.py migrate --noinput
    twin up -d
    wait_healthy
    ;;
  *)
    echo "usage: $0 up|refresh-db" >&2
    exit 2
    ;;
esac
