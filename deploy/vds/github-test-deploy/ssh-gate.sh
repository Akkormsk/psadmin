#!/bin/sh
set -eu

if [ "${SSH_ORIGINAL_COMMAND:-}" != "deploy-test" ]; then
  echo "Only deploy-test is permitted." >&2
  exit 1
fi

exec /usr/bin/sudo -n /usr/local/sbin/psadmin-test-deploy
