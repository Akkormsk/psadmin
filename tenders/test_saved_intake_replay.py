"""Optional local replay of a SELECT-only TEST snapshot. Snapshots stay outside Git."""
import json
import os
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.core import serializers
from django.core.management.color import no_style
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from tender_selection.models import DocumentPreview, Tender
from tender_selection.services import _run_document_analysis
from .calculation_v2_pipeline import _active_source_ancestors, source_route_snapshots
from .models import ComponentRoutePlan, TenderComputeJob, TenderSourceItem


@skipUnless(os.getenv('PSADMIN_OFFLINE_REPLAY_FILE'), 'Requires an explicitly supplied local saved snapshot')
@override_settings(CALCULATION_V2_SOURCE_MEMORY_ENABLED=True, CALCULATION_V2_PREPARATION_ENABLED=False)
class SavedIntakeReplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.snapshot = json.loads(Path(os.environ['PSADMIN_OFFLINE_REPLAY_FILE']).read_text())
        loaded_models = set()
        with connection.constraint_checks_disabled():
            for record in serializers.deserialize('json', json.dumps(cls.snapshot['fixtures'])):
                for field in record.object._meta.fields:
                    if (getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False)) and getattr(record.object, field.attname) is None:
                        setattr(record.object, field.attname, timezone.now())
                record.save()
                loaded_models.add(type(record.object))
        connection.check_constraints()
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), loaded_models):
                cursor.execute(sql)

    def test_complete_chain_preserves_saved_sources_facts_and_routes_across_tenders(self):
        answers = {int(key): value for key, value in self.snapshot['owner_answers'].items()}
        reports = []
        # Only responses already stored in stage caches may be reused. A missing saved
        # answer stays missing; this replay never invents a route or calls a provider.
        with (patch('threading.Thread'), patch('urllib.request.urlopen', side_effect=AssertionError('Offline replay')),
             override_settings(CALCULATION_V2_CONTEXT_LIMITS=self.snapshot['context_limits']),
             patch('tenders.gateway_budget.model_catalog', return_value=self.snapshot['model_catalog']),
             patch('tender_selection.services.documents_html'), patch('tender_selection.services.extras_for'),
             patch('tender_selection.services._refresh_intake_profile', return_value='review'),
             patch('tenders.calculation_v2_pipeline._knowledge_for', return_value=self.snapshot['knowledge']),
             patch('tenders.calculation_v2_pipeline._route_owner_answer_map', side_effect=lambda sources: {source.pk: answers.get(source.pk, []) for source in sources}),
             patch('tenders.services._ai_gateway_json', return_value=({'items': []}, {})) as gateway):
            for tender in Tender.objects.filter(pk__in=self.snapshot['tender_ids']).order_by('pk'):
                with self.subTest(tender=tender.purchase_number):
                    before_sources = set(TenderSourceItem.objects.filter(tender=tender).values_list('pk', flat=True))
                    inactive_ancestors = set(TenderSourceItem.objects.filter(pk__in=_active_source_ancestors(tender),
                        is_active=False).values_list('pk', flat=True))
                    before_routes = {plan.pk: list(plan.steps.values_list('pk', 'process_id', 'position'))
                        for plan in ComponentRoutePlan.objects.filter(commercial_item__tender=tender)}
                    before_facts = {preview.pk: preview.extraction.get('facts', {}) for preview in
                        DocumentPreview.objects.filter(tender_versions__tender=tender).distinct()}
                    calls_before = gateway.call_count
                    ready_before = {row['plan_id'] for row in source_route_snapshots(tender).values() if row['state'] == 'ready'}
                    job = TenderComputeJob.objects.create(tender=tender, version='v2-intake-refresh', trigger='manual_document_refresh')
                    with patch('tender_selection.services.notification_for', return_value=tender.notification_raw):
                        _run_document_analysis(job.pk)
                    job.refresh_from_db()
                    self.assertFalse(job.error, {'number': tender.purchase_number, 'error': job.error})
                    self.assertIn('routing', job.diagnostics['stages'])
                    self.assertTrue(before_sources <= set(TenderSourceItem.objects.filter(tender=tender).values_list('pk', flat=True)))
                    self.assertFalse(TenderSourceItem.objects.filter(pk__in=inactive_ancestors, is_active=True).exists())
                    for pk, steps in before_routes.items():
                        self.assertEqual(list(ComponentRoutePlan.objects.get(pk=pk).steps.values_list('pk', 'process_id', 'position')), steps)
                    for pk, facts in before_facts.items():
                        after = DocumentPreview.objects.get(pk=pk).extraction.get('facts', {})
                        self.assertTrue(all(after.get(key) == value for key, value in facts.items()))
                    routes = source_route_snapshots(tender)
                    self.assertTrue(ready_before <= {row['plan_id'] for row in routes.values() if row['state'] == 'ready'})
                    ready_components = set(ComponentRoutePlan.objects.filter(pk__in=[row['plan_id'] for row in routes.values()
                        if row['state'] == 'ready']).values_list('component_id', flat=True))
                    first_calls = gateway.call_count - calls_before
                    repeat = TenderComputeJob.objects.create(tender=tender, version='v2-intake-refresh',
                        trigger='manual_document_refresh', diagnostics={'routes_only': True})
                    repeat_calls = gateway.call_count
                    _run_document_analysis(repeat.pk)
                    repeat.refresh_from_db()
                    self.assertFalse(repeat.error)
                    for call in gateway.call_args_list[repeat_calls:]:
                        task = json.loads(call.args[0].rsplit('\n', 1)[1])
                        self.assertFalse(ready_components.intersection(row['component_id'] for row in task.get('items', [])))
                    reports.append({'tender': tender.purchase_number, 'status': job.status,
                        'routes': {state: sum(row['state'] == state for row in routes.values()) for state in {row['state'] for row in routes.values()}},
                        'saved_plans_preserved': len(before_routes), 'missing_saved_responses': first_calls,
                        'route_repeat_missing_responses': gateway.call_count - repeat_calls,
                        'routing_reused': job.diagnostics.get('routing_reused_count', 0), 'gaps': len(job.diagnostics.get('analysis_gaps', [])),
                        'issues': [{'stage': gap.get('stage'), 'reason': gap.get('reason', '')} for gap in job.diagnostics.get('analysis_gaps', [])]})
        report_path = os.environ.get('PSADMIN_OFFLINE_REPLAY_REPORT')
        if report_path:
            Path(report_path).write_text(json.dumps({'reports': reports, 'paid_calls': 0}, ensure_ascii=False, indent=2))
