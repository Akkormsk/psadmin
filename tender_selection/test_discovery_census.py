import json
import os
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from . import gosplan, services
from .models import FilterSettings, Tender, TenderHistory
from .primary_search import word_pattern, split_primary_requests


class CensusTests(TestCase):
    def test_explicit_full_scan_does_not_reuse_incremental_cursor(self):
        settings=FilterSettings.load()
        settings.include_words='сувенир'
        settings.laws=['fz44']
        settings.save()
        with patch.object(gosplan,'iter_purchases',side_effect=lambda **kw: iter([])) as source:
            services.run_pull()
            services.run_pull()
            self.assertIn('updated_after',source.call_args.kwargs['params'])
            result=services.run_pull(full_scan=True)
            self.assertNotIn('updated_after',source.call_args.kwargs['params'])
            self.assertTrue(result.params['full_scan'])
            self.assertTrue(result.params['complete'])

    def test_census_retries_counts_deduplicates_and_uses_normal_archive_without_ai(self):
        now=timezone.now()
        preset={'include_words':'сувенир, брендир','append_exclude_words':'медал',
                'min_price':'300000','laws':['fz44'],'sources':['eis'],
                'regions':[],'okpd2_codes':[],
                'primary_filters':{'stage':['1'],'max_price_le':'2000000',
                    'published_after':(now-timedelta(days=1)).isoformat()}}
        record={'purchase_number':'123','object_info':'Сувенирные медали',
                'max_price':500000,'collecting_finished_at':(now+timedelta(days=2)).isoformat()}
        output=StringIO()
        with TemporaryDirectory() as directory:
            p=Path(directory)/'preset.json';p.write_text(json.dumps(preset))
            with patch.dict(os.environ,{'GOSPLAN_API_DISABLED':'0','TENDER_DISCOVERY_ROLE':'standalone',
                    'TENDER_DISCOVERY_FEED_DIR':'','GOSPLAN_RATE_RETRIES':'1'}), \
                 patch.object(gosplan,'fetch_page',side_effect=[gosplan.RateLimitError('429'),[record,record],[record]]) as source, \
                 patch.object(gosplan.time,'sleep'), \
                 patch('tender_selection.management.commands.census_discovery.signal.alarm'), \
                 patch('tender_selection.profile_triage.start_profile_triage_in_background') as ai, \
                 patch('tenders.calculation_v2_pipeline.trigger_visible_tender') as compute:
                call_command('census_discovery',preset=str(p),passes=2,max_seconds=60,stdout=output)
        self.assertEqual(source.call_args_list[0].args,source.call_args_list[1].args)
        self.assertEqual(Tender.objects.count(),1)
        self.assertEqual(TenderHistory.objects.count(),1)
        self.assertEqual(Tender.objects.get().status,Tender.DISMISSED)
        self.assertEqual(TenderHistory.objects.get().reason_code,'minus_words')
        ai.assert_not_called();compute.assert_not_called()
        result=json.loads(output.getvalue().splitlines()[-1])['census_result']
        self.assertTrue(result['complete'])
        self.assertEqual(result['http_attempts_total'],3)
        self.assertEqual(result['passes'][0]['http_errors'],1)
        self.assertEqual(result['passes'][0]['unique_records'],1)
        self.assertTrue(result['passes'][1]['same_notice_set'])
        self.assertEqual(result['passes'][1]['created'],0)

    def test_prod_or_disabled_process_cannot_start_census(self):
        for role,disabled in [('collector','0'),('standalone','1')]:
            with patch.dict(os.environ,{'TENDER_DISCOVERY_ROLE':role,'GOSPLAN_API_DISABLED':disabled}), self.assertRaises(CommandError):
                call_command('census_discovery',preset='not-used.json',stdout=StringIO())


class SouvenirPresetTests(SimpleTestCase):
    def test_three_provider_groups_and_title_roots_only(self):
        preset=json.loads(Path('deploy/vds/souvenir-discovery.json').read_text())
        groups=split_primary_requests({'object_info':word_pattern(preset['include_words'])},preset['include_words'],'fz44')
        self.assertEqual(len(groups),3)
        self.assertTrue(all(len(q['object_info'])<=128 for q in groups))
        self.assertEqual(preset['okpd2_codes'],[])
        self.assertNotIn('павербанк',preset['include_words'])


class ProductionPauseLeaseTests(SimpleTestCase):
    def test_real_quota_lock_releases_on_request_or_timeout(self):
        import fcntl
        import subprocess
        import sys
        from scripts.run_discovery_census import LEASE
        with TemporaryDirectory() as directory:
            env={**os.environ,'CENSUS_TEST_LOCK_ROOT':directory}
            for release in (True,False):
                child=subprocess.Popen([sys.executable,'-u','-c',LEASE,'1'],env=env,
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(),'PROD_GOSPLAN_PAUSED')
                    with (Path(directory)/'.quota-lock').open('a+') as lock:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        if release:
                            child.stdin.write('release\n');child.stdin.flush()
                        child.wait(timeout=3)
                        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally:
                    if child.poll() is None:child.kill();child.wait()
                    child.stdin.close();child.stdout.close()
