#!/usr/bin/env bash
# Nightly from the deploy user's crontab: dump locally (14 days), copy to Timeweb S3 (30 days).
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
mkdir -p "$HOME/backups"

export RCLONE_CONFIG_S3_TYPE=s3 RCLONE_CONFIG_S3_PROVIDER=Other RCLONE_CONFIG_S3_REGION=ru-1
export RCLONE_CONFIG_S3_ENDPOINT="$S3_ENDPOINT"
export RCLONE_CONFIG_S3_ACCESS_KEY_ID="$S3_ACCESS_KEY" RCLONE_CONFIG_S3_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
s3() {
  docker run --rm -v "$HOME/backups:/backups:ro" \
    -e RCLONE_CONFIG_S3_TYPE -e RCLONE_CONFIG_S3_PROVIDER -e RCLONE_CONFIG_S3_REGION -e RCLONE_CONFIG_S3_ENDPOINT \
    -e RCLONE_CONFIG_S3_ACCESS_KEY_ID -e RCLONE_CONFIG_S3_SECRET_ACCESS_KEY \
    rclone/rclone:1 --log-level ERROR "$@"
}

name="psadmin-$(date +%Y%m%d-%H%M).dump"
docker compose exec -T db pg_dump -U psadmin -d psadmin -Fc > "$HOME/backups/$name"
s3 copy "/backups/$name" "s3:$S3_BUCKET/db/"
s3 delete --min-age 30d --include 'psadmin-*.dump' "s3:$S3_BUCKET/db/"
find "$HOME/backups" -name 'psadmin-*.dump' -mtime +14 -delete
echo "$(date -Is) $name uploaded"
