from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from .models import Tender, TenderHistory, FilterSettings
from . import services, gosplan


class SecondaryIntakeTests(TestCase):
    def setUp(self):
        self.selection = FilterSettings.load()
        self.selection.include_words = 'полиграф'
        self.selection.exclude_words = 'ремонт+дорог, щебень'
        self.selection.min_price = 300000
        self.selection.laws = ['fz44']
        self.selection.okpd2_codes = []
        self.selection.save()

    def record(self, number='123', title='Бумага', **extra):
        return dict(purchase_number=number, object_info=title, max_price=1,
                    collecting_finished_at=(timezone.now()+timedelta(days=2)).isoformat(), **extra)

    def pull(self, record):
        with patch.object(gosplan, 'iter_purchases', return_value=iter([record])), \
             patch('tender_selection.profile_triage.start_profile_triage_in_background'), \
             patch('tenders.calculation_v2_pipeline.trigger_visible_tender'):
            run=services.run_pull()
        self.assertTrue(run.ok, run.error)
        return Tender.objects.get(purchase_number=record['purchase_number'])

    def test_primary_words_and_price_are_not_rechecked_locally(self):
        tender=self.pull(self.record())
        self.assertEqual(tender.status, Tender.NEW)
        self.assertIs(tender.history_record.filtered_out, False)
        rows,hidden,_=services.visible_found_tenders(Tender.objects.all(), min_price=9999999,
            include_words='несовпадение', exclude_words='бумага')
        self.assertEqual([r.pk for r in rows], [tender.pk])
        self.assertEqual(hidden, 0)

    def test_secondary_compound_archives_and_records_reason_without_enrichment(self):
        tender=self.pull(self.record(title='РЕМОНТ автомобильных ДОРОГ'))
        self.assertEqual(tender.status, Tender.DISMISSED)
        history=tender.history_record
        self.assertEqual(history.reason_code, 'minus_words')
        self.assertEqual(history.minus_hits, ['ремонт+дорог'])
        self.assertTrue(history.rule_version)
        self.assertIn('intake',history.filter_snapshot)
        self.assertFalse(tender.notification_raw)
        self.assertEqual(TenderHistory.objects.filter(tender=tender).count(), 1)

    def test_one_part_of_compound_does_not_reject(self):
        self.assertEqual(self.pull(self.record(title='Ремонт принтера')).status,Tender.NEW)

    def test_settings_and_external_updates_do_not_reclassify_admitted_card(self):
        tender=self.pull(self.record())
        snapshot=tender.history_record.filter_snapshot
        self.selection.exclude_words='бумага, щебень'
        self.selection.save()
        updated=self.pull(self.record(title='Щебень'))
        self.assertEqual(updated.status,Tender.NEW)
        self.assertEqual(updated.history_record.filter_snapshot,snapshot)

    def test_restore_survives_unchanged_negative_rule_and_repeat(self):
        tender=self.pull(self.record(title='Щебень'))
        admin=get_user_model().objects.create_superuser('intake-admin', password='test')
        services.restore_hidden_tender(admin,tender.pk)
        tender=self.pull(self.record(title='Щебень'))
        self.assertEqual(tender.status,Tender.NEW)
        rows,_,_=services.visible_found_tenders(Tender.objects.filter(pk=tender.pk), min_price=300000,
            include_words='полиграф',exclude_words='щебень')
        self.assertEqual(len(rows),1)

    def test_expired_arrival_goes_to_same_archive(self):
        record=self.record()
        record['collecting_finished_at']=(timezone.now()-timedelta(hours=1)).isoformat()
        tender=self.pull(record)
        self.assertEqual(tender.status,Tender.DISMISSED)
        self.assertEqual(tender.history_record.reason_code,'expired')

    def test_bootstrap_preserves_legacy_cards_without_reapplying_filter(self):
        from .intake import initialize_existing
        opened=Tender.objects.create(purchase_number='opened',title='Щебень',opened_at=timezone.now())
        untouched=Tender.objects.create(purchase_number='untouched',title='Щебень')
        first=initialize_existing(apply=True)
        opened.refresh_from_db();untouched.refresh_from_db()
        self.assertEqual(opened.status,Tender.NEW)
        self.assertEqual(untouched.status,Tender.NEW)
        self.assertEqual(first['decided'],2)
        self.assertEqual(initialize_existing(apply=True)['decided'],0)

    def test_failed_secondary_decision_rolls_back_card_and_history(self):
        with patch.object(gosplan, 'iter_purchases', return_value=iter([self.record()])), \
             patch('tender_selection.intake.decide', side_effect=ValueError('cannot decide')):
            run = services.run_pull()
        self.assertFalse(run.ok)
        self.assertFalse(Tender.objects.exists())
        self.assertFalse(TenderHistory.objects.exists())
        self.assertFalse(run.params['complete'])

    def test_provider_deadline_extension_reopens_only_automatic_expiry(self):
        record=self.record()
        record['collecting_finished_at']=(timezone.now()-timedelta(hours=1)).isoformat()
        tender=self.pull(record)
        self.assertEqual(tender.status,Tender.DISMISSED)
        reopened=self.pull(self.record())
        self.assertEqual(reopened.status,Tender.NEW)
        self.assertEqual(reopened.history_record.reason_code,'')

    def test_extension_does_not_undo_negative_or_manual_archive(self):
        record=self.record(title='Щебень')
        record['collecting_finished_at']=(timezone.now()-timedelta(hours=1)).isoformat()
        tender=self.pull(record)
        self.assertEqual(self.pull(self.record(title='Щебень')).status,Tender.DISMISSED)
        second=self.pull(self.record(number='manual'))
        services.dismiss_tender(second)
        self.assertEqual(self.pull(self.record(number='manual')).status,Tender.DISMISSED)

    def test_manual_hide_of_expired_card_blocks_automatic_reopening(self):
        record = self.record()
        record['collecting_finished_at'] = (timezone.now()-timedelta(hours=1)).isoformat()
        tender = self.pull(record)
        services.dismiss_tender(tender)
        self.assertEqual(self.pull(self.record()).status, Tender.DISMISSED)

    def test_disabled_provider_is_paused_without_any_search_requests(self):
        import os
        with patch.dict(os.environ, {'GOSPLAN_API_DISABLED':'1'}), patch.object(gosplan,'iter_purchases') as fetch:
            run = services.run_pull()
        fetch.assert_not_called()
        self.assertTrue(run.params['paused'])
        self.assertFalse(Tender.objects.exists())
