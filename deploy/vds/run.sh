#!/usr/bin/env bash
# One-off manage.py command in its own container, so heavy jobs never share the web process memory.
set -euo pipefail
cd "$(dirname "$0")"
exec docker compose run --rm -T --no-deps \
  -e MODULBANK_AUTOSYNC_ENABLED=0 -e OASIS_AUTOSYNC_ENABLED=0 \
  web python manage.py "$@"
