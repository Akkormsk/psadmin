#!/usr/bin/env bash
# Wrapped in main so bash parses the whole script before git pull rewrites this file.
set -euo pipefail

main() {
  cd "$(dirname "$0")"
  git -C ../.. pull --ff-only origin main
  git -C ../.. log --oneline -1
  docker compose build web
  docker compose run --rm -T web python manage.py migrate --noinput
  docker compose up -d
  local status=starting
  for _ in $(seq 1 30); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q web)")
    if [ "$status" = healthy ]; then
      echo "web: healthy"
      return 0
    fi
    sleep 3
  done
  echo "web: $status" >&2
  docker compose logs --tail 50 web >&2
  return 1
}

main "$@"
exit
