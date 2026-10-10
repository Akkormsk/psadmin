# Restricted GitHub test deploy

Install these files once as root. They preserve the existing `twin.sh up` order:
build `test-web`, start `test-db`, run migrations, start all test services, then
wait for `test-web` health. They deliberately do not include `refresh-db`.

The GitHub archive is application source only. The root-owned wrapper rejects
deployment files and always uses the installed Compose file, Dockerfile, and
certificate files.
