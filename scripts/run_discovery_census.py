"""Bounded TEST census under an automatically released PROD quota lock.

Executed on the existing VDS by Actions, never on a developer's machine.
No PROD deploy, settings write, container restart, or application DB access here.
"""
import argparse
import json
import select
import subprocess
import sys
from datetime import datetime, timezone

PROD = 'psadmin-web-1'
TEST = 'psadmin-test-test-web-1'

LEASE = r'''
import fcntl, os, signal, sys
from pathlib import Path
signal.alarm(int(sys.argv[1]))
if os.getenv("CENSUS_TEST_LOCK_ROOT"):
    root = Path(os.environ["CENSUS_TEST_LOCK_ROOT"])
else:
    from tender_selection.discovery_quota import _directory
    root = _directory()
with (root/'.quota-lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print('PROD_GOSPLAN_PAUSED', flush=True)
    sys.stdin.readline()
'''

CHECK = r'''
import inspect, json, os
from pathlib import Path
from tender_selection import gosplan
from tender_selection.scheduler import _web_autopull_enabled
source = inspect.getsource(gosplan._get)
assert 'path not in {"/fz44/purchases", "/fz223/purchases"}' in source
for name in ('fetch_organization','fetch_notification','fetch_clarifications','fetch_complaints','fetch_purchase','fetch_contracts'):
    assert '_direct_eis(' in inspect.getsource(getattr(gosplan,name)), name
assert not _web_autopull_enabled(), 'Web background collection must be disabled during census'
print(json.dumps({'secondary_gosplan_calls_blocked':True,'web_autopull':False,
    'role':os.getenv('TENDER_DISCOVERY_ROLE'),'api_disabled':os.getenv('GOSPLAN_API_DISABLED'),
    'release':Path('/app/.release-sha').read_text().strip() if Path('/app/.release-sha').exists() else Path('/app/.twin-version').read_text().strip()}))
'''


def inspect_clients():
    names = subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines()
    for name in names:
        if not name.startswith('psadmin'):
            continue
        data = json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
        env = dict(entry.split('=',1) for entry in data['Config'].get('Env',[]) if '=' in entry)
        role = env.get('TENDER_DISCOVERY_ROLE')
        if role not in {'collector','standalone','mirror'}:
            continue
        disabled = env.get('GOSPLAN_API_DISABLED') == '1'
        print(json.dumps({'container':name,'role':role,'gosplan_disabled':disabled}),flush=True)
        assert disabled or role == 'mirror' or name in {PROD,'psadmin-discovery-collector-1'}, 'Unexpected active source client'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-seconds',type=int,default=1800)
    parser.add_argument('--run-id',required=True)
    args = parser.parse_args()
    assert 0 < args.max_seconds <= 3600
    assert args.run_id.isdecimal()
    inspect_clients()
    for container in (PROD,TEST):
        subprocess.run(['docker','exec',container,'python','-c',CHECK],check=True)
    lease = subprocess.Popen(['docker','exec','-i','-e','CENSUS_TEST_LOCK_ROOT=',PROD,'python','-u','-c',LEASE,str(args.max_seconds+150)],
                             stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        ready,_,_ = select.select([lease.stdout],[],[],120)
        if not ready or lease.stdout.readline().strip() != 'PROD_GOSPLAN_PAUSED':
            raise RuntimeError('Could not acquire bounded PROD pause; census not started')
        print(json.dumps({'prod_gosplan_paused':True,'at':datetime.now(timezone.utc).isoformat(),
            'automatic_release_seconds':args.max_seconds+150}),flush=True)
        # No new PROD attempt can cross its shared limiter while this lease is held.
        # Wait for any HTTP request reserved immediately before the lease to finish.
        import time
        time.sleep(30)
        command = ['docker','exec',
            '-e','GOSPLAN_API_DISABLED=0','-e','TENDER_DISCOVERY_ROLE=standalone',
            '-e','GOSPLAN_REQUESTS_PER_MINUTE=9',
            '-e','GOSPLAN_QUOTA_DIR=/tmp/psadmin-census-'+args.run_id,
            '-e','GOSPLAN_CLIENT_ID=census-'+args.run_id,
            '-e','TENDER_DISCOVERY_ENRICH_AFTER_PULL=0',
            '-e','EAT_PULL_ENABLED=0','-e','TENDER_AUTOPULL_ENABLED=0',
            '-e','TENDER_AUTOPULL_WEB_ENABLED=0','-e','CALCULATION_V2_ENABLED=0',
            TEST,'python','manage.py','census_discovery','--preset','deploy/vds/souvenir-discovery.json',
            '--max-seconds',str(args.max_seconds),'--passes','2']
        subprocess.run(command,check=True,timeout=args.max_seconds+60)
    finally:
        if lease.stdin:
            try:
                lease.stdin.write('release\n')
                lease.stdin.flush()
                lease.stdin.close()
            except BrokenPipeError:
                pass
        lease.wait(timeout=10)
        print(json.dumps({'prod_gosplan_resumed':True,'at':datetime.now(timezone.utc).isoformat()}),flush=True)


if __name__ == '__main__':
    main()
