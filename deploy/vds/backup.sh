#!/usr/bin/env bash
# Nightly from the deploy user's crontab; dumps stay on this server's disk.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p "$HOME/backups"
name="psadmin-$(date +%Y%m%d-%H%M).dump"
docker compose exec -T db pg_dump -U psadmin -d psadmin -Fc > "$HOME/backups/$name"
find "$HOME/backups" -name 'psadmin-*.dump' -mtime +14 -delete
echo "$name"
