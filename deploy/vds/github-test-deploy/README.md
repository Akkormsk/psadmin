# Restricted GitHub test deploy

Install these files once as root. They preserve the existing `twin.sh up` order:
build `test-web`, start `test-db`, run migrations, start all test services, then
wait for `test-web` health. They deliberately do not include `refresh-db`.

The GitHub archive is application source only. The root-owned wrapper rejects
deployment files and always uses the installed Compose file, Dockerfile, and
certificate files.

## One-time VDS installation

Run as root from a checked-out copy of this repository. Replace `PUBLIC_KEY`
with the public half of the dedicated GitHub Actions key.

```bash
repo=/path/to/psadmin
install -d -m 0755 /etc/psadmin-test/certs
install -m 0755 "$repo/deploy/vds/github-test-deploy/ssh-gate.sh" /usr/local/sbin/psadmin-test-ssh-gate
install -m 0755 "$repo/deploy/vds/github-test-deploy/psadmin-test-deploy" /usr/local/sbin/psadmin-test-deploy
install -m 0755 "$repo/deploy/vds/github-test-deploy/unpack-release.py" /etc/psadmin-test/unpack-release.py
install -m 0644 "$repo/deploy/vds/github-test-deploy/compose.test.yml" /etc/psadmin-test/compose.test.yml
install -m 0644 "$repo/deploy/vds/github-test-deploy/Dockerfile" /etc/psadmin-test/Dockerfile
install -m 0644 "$repo/deploy/vds/certs/"*.crt /etc/psadmin-test/certs/
install -m 0440 "$repo/deploy/vds/github-test-deploy/psadmin-test-deploy.sudoers" /etc/sudoers.d/psadmin-test-deploy
visudo -cf /etc/sudoers.d/psadmin-test-deploy
printf '%s\n' 'restrict,command="/usr/local/sbin/psadmin-test-ssh-gate" PUBLIC_KEY' >> /home/deploy/.ssh/authorized_keys
```

Before the first Actions deploy, save the existing test tree as the first rollback
release. This does not run Docker, migrations, or `refresh-db`.

```bash
base=/home/deploy/psadmin-test
legacy="$base/releases/pre-github-actions"
install -d -m 0755 "$legacy"
tar -C "$base" --exclude=./releases --exclude=./current --exclude=./previous -cf - . | tar -C "$legacy" -xf -
install -d -m 0755 "$legacy/.deploy-trusted"
cp -a /etc/psadmin-test/certs "$legacy/.deploy-trusted/certs"
printf 'legacy\n' > "$legacy/.psadmin-release-tag"
ln -sfn "$legacy" "$base/current"
```

The deploy wrapper uses `sudo` only for its fixed root-owned path. It is this
sudoers rule—not file ownership—that gives it the Docker access required to
preserve `twin.sh up` behaviour.
