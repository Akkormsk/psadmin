import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase
from django.core.management import call_command, CommandError
from io import StringIO
from . import gosplan, discovery_quota


class IndependentDiscoveryTests(SimpleTestCase):
    def test_shared_quota_and_local_receipts_do_not_import_feed(self):
        with TemporaryDirectory() as shared, TemporaryDirectory() as local, patch.dict(os.environ, {
            'GOSPLAN_QUOTA_DIR': shared, 'GOSPLAN_RECEIPTS_DIR': local, 'GOSPLAN_CLIENT_ID': 'test',
            'TENDER_DISCOVERY_FEED_DIR': local, 'GOSPLAN_REQUESTS_PER_MINUTE': '9'}):
            with patch.object(discovery_quota.time, 'time', return_value=100), patch.object(discovery_quota.time, 'sleep') as sleep:
                discovery_quota.reserve_request('/fz44/purchases', {})
                discovery_quota.record_response(429, {'Retry-After':'10'})
                discovery_quota.reserve_request('/fz44/purchases', {})
            self.assertEqual(float(Path(shared,'.provider-wait-until').read_text()),110)
            self.assertEqual(sleep.call_args.args[0],10)
            master=Path(shared,'requests.jsonl').read_text()
            self.assertEqual(master,Path(local,'requests.jsonl').read_text())
            self.assertTrue(all(json.loads(line)['client']=='test' for line in master.splitlines()))
            self.assertFalse(list(Path(local).glob('*.json')))

    def test_429_retries_same_page_and_504_splits_without_missing_range(self):
        params={'published_after':'2026-11-01T00:00:00+00:00','published_before':'2026-11-15T00:00:00+00:00'}
        with patch.object(gosplan,'fetch_page',side_effect=[gosplan.RateLimitError('429'),[]]) as fetch, \
             patch.object(gosplan.time,'sleep'):
            self.assertEqual(list(gosplan.iter_purchases(params=params)),[])
        self.assertEqual(fetch.call_args_list[0].args,fetch.call_args_list[1].args)
        with patch.object(gosplan,'fetch_page',side_effect=[gosplan.GosplanError('HTTP 504'),[],[]]) as fetch:
            self.assertEqual(list(gosplan.iter_purchases(params=params)),[])
        newer,older=[c.args[0] for c in fetch.call_args_list[1:]]
        self.assertEqual(newer['published_before'],params['published_before'])
        self.assertEqual(older['published_after'],params['published_after'])
        self.assertEqual(newer['published_after'],older['published_before'])

    def test_collector_does_not_run_secondary_network_or_ai(self):
        from types import SimpleNamespace
        run=SimpleNamespace(created_count=0,updated_count=0,requests_made=1,params={'complete':True},ok=True,error='')
        with patch.dict(os.environ,{'TENDER_DISCOVERY_ROLE':'standalone'}), \
             patch('tender_selection.management.commands.collect_discovery.run_pull',return_value=run) as pull, \
             patch('tender_selection.management.commands.collect_discovery.expire_incoming') as expire:
            call_command('collect_discovery',stdout=StringIO(),max_seconds=10)
        pull.assert_called_once_with(max_seconds=10)
        self.assertEqual(expire.call_count,2)
        with patch.dict(os.environ,{'TENDER_DISCOVERY_ROLE':'mirror'}), self.assertRaises(CommandError):
            call_command('collect_discovery',stdout=StringIO())
