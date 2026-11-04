"""Restore a verified production backup into a new private TEST database only."""
import json
import pathlib
import re
import shutil
import subprocess
import time

run_id = str(int(time.time()))
name = 'verify_intake_' + run_id
if not re.fullmatch(r'verify_intake_\d+', name):
    raise SystemExit('Invalid isolated database name')
compose = ['docker','compose','-f','/home/deploy/psadmin-test/deploy/vds/compose.test.yml']
backups = sorted((pathlib.Path.home()/'backups').glob('psadmin-*.dump'),key=lambda path:path.stat().st_mtime,reverse=True)
if not backups:
    raise SystemExit('Production backup is missing')
backup = backups[0]
if shutil.disk_usage('/home/deploy').free < max(2_000_000_000,backup.stat().st_size*10):
    raise SystemExit('Insufficient free disk for isolated restore; nothing created')
helper = pathlib.Path('/tmp/verify_restored_data.py').read_bytes()

def run(arguments, **kwargs):
    return subprocess.run(compose + arguments, check=True, timeout=900, **kwargs)

created = False
try:
    run(['exec','-T','test-db','createdb','-U','psadmin',name]); created=True
    with backup.open('rb') as source:
        run(['exec','-T','test-db','pg_restore','-U','psadmin','-d',name,'--no-owner','--no-privileges','--exit-on-error'],stdin=source)
    base = ['exec','-T','-e','DB_NAME='+name,'-e','CALCULATION_V2_ENABLED=0','-e','CALCULATION_V2_PREPARATION_ENABLED=0','-e','TENDER_AUTOPULL_ENABLED=0']
    run(base + ['-e','VERIFY_MODE=before','test-web','python','-'],input=helper)
    run(base + ['test-web','python','manage.py','migrate','--noinput'])
    run(base + ['-e','VERIFY_MODE=after','test-web','python','-'],input=helper)
    print(json.dumps({'restore_verified':True,'backup':backup.name,'live_database_changed':False,'existing_test_database_changed':False}),flush=True)
finally:
    if created:
        run(['exec','-T','test-db','dropdb','-U','psadmin',name,'--force'])
