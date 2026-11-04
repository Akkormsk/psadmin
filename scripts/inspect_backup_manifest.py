"""Verify a local PostgreSQL backup catalogue without restoring or exposing data."""
import datetime
import json
import pathlib
import subprocess

backups = sorted((pathlib.Path.home() / 'backups').glob('psadmin-*.dump'), key=lambda path:path.stat().st_mtime, reverse=True)
if not backups:
    raise SystemExit('No production backup found')
latest = backups[0]
with latest.open('rb') as source:
    result = subprocess.run(['docker','compose','-f','/home/deploy/psadmin/deploy/vds/compose.yml','exec','-T','db','pg_restore','--list'], stdin=source, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=90)
print(json.dumps({'backup':latest.name,'bytes':latest.stat().st_size,
    'created_utc':datetime.datetime.fromtimestamp(latest.stat().st_mtime,datetime.timezone.utc).isoformat(),
    'catalogue_valid':result.returncode==0,'backup_count':len(backups)}), flush=True)
if result.returncode:
    raise SystemExit('Backup catalogue validation failed')
