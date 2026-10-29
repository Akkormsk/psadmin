from unittest.mock import patch
from decimal import Decimal
from django.test import TestCase
from .services import create_tender_estimate, decide_document_proposals
from tenders import test_document_components as fixtures
from tenders.models import TenderEstimate


class EstimateSourceTransferTests(TestCase):
    setUp = fixtures.DocumentComponentAcceptanceTests.setUp
    row = fixtures.DocumentComponentAcceptanceTests.row
    stage = fixtures.DocumentComponentAcceptanceTests.stage

    def create(self):
        with patch('tender_selection.stats.price_stats_for', return_value=None):
            return TenderEstimate.objects.get(pk=create_tender_estimate(self.admin, self.tender.pk)['estimate_id'])

    def test_accepted_name_and_complete_requirements_replace_raw_notification(self):
        parent = self.parents[0]
        parent.requirements = {'characteristics': [{'name': 'Материал', 'value': 'металл'}]}
        parent.save(update_fields=['requirements'])
        self.stage([self.row(parent, relationship='enrich', quantity=100, name='Ручка', requirements={'characteristics': [{'name': 'Нанесение', 'value': 'гравировка'}]})])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        estimate = self.create()
        line = estimate.lines.get(name='Ручка')
        self.assertEqual(line.quantity, Decimal('100'))
        self.assertEqual(line.nmck_unit, Decimal('1000'))
        facts = {row['label']: row['value'] for row in line.requirements['requirements']}
        self.assertEqual(facts, {'Материал': 'металл', 'Нанесение': 'гравировка'})
        self.assertTrue(line.requirements['source_item_id'])
        self.assertEqual(line.requirements['source_name'], 'Ручка')
        import json
        from django.urls import reverse
        response = self.client.get(reverse('tender_pipeline_estimate', args=[estimate.pk]))
        self.assertEqual(response.status_code, 200)
        displayed = next(row for row in json.loads(response.context['initial_lines_json']) if row['name'] == 'Ручка')
        self.assertEqual(displayed['requirements']['requirements'], line.requirements['requirements'])

    def test_unaccepted_proposals_are_not_used_and_existing_calculation_is_not_overwritten(self):
        self.stage([self.row(self.parents[0], name='Не принято')])
        estimate = self.create()
        self.assertFalse(estimate.lines.filter(name='Не принято').exists())
        line = estimate.lines.first(); line.name = 'Ручное изменение'; line.save(update_fields=['name'])
        repeated = self.create()
        self.assertEqual(repeated.pk, estimate.pk)
        self.assertTrue(repeated.lines.filter(name='Ручное изменение').exists())

    def test_components_keep_own_quantities_and_requirements_without_invented_unit_quote(self):
        parent = self.parents[0]
        self.stage([self.row(parent, name='Ручка'), self.row(parent, name='Блокнот')])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        estimate = self.create()
        for line in estimate.lines.filter(name__in=['Ручка', 'Блокнот']):
            self.assertEqual(line.quantity, Decimal('200'))
            self.assertEqual(line.nmck_unit, 0)
            self.assertTrue(line.requirements['nmck_unit_unknown'])
            self.assertEqual(line.requirements['requirements'][0]['label'], 'Цвет')
        references = estimate.summary_snapshot['source_financials']
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]['sum'], '100000')

    def test_operator_refresh_changes_only_untouched_legacy_name_and_requirements(self):
        from scripts.sync_test_estimate_requirements import collect_plan
        estimate = self.create()
        parent = self.parents[0]
        self.stage([self.row(parent, relationship='enrich', quantity=100, name='Ручка')])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        plan = collect_plan()
        self.assertEqual(len(plan['updates']), 1)
        self.assertFalse(plan['skipped'])
        for change in plan['updates']:
            self.assertEqual(set(change['after']), {'name', 'requirements'})
        line = estimate.lines.first()
        line.name = 'Мой вариант'; line.save(update_fields=['name'])
        plan = collect_plan()
        self.assertFalse(plan['updates'])
        self.assertEqual(plan['skipped'][0]['reason'], 'manual_name')

    def test_operator_apply_is_idempotent_and_preserves_financial_fields(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from scripts.sync_test_estimate_requirements import collect_plan, digest, main
        estimate = self.create()
        parent = self.parents[0]
        self.stage([self.row(parent, relationship='enrich', quantity=100, name='Ручка')])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        # Exercise the legacy row without provenance fields.
        line = estimate.lines.get(sort_order=0)
        line.requirements = {}; line.save(update_fields=['requirements'])
        before = list(estimate.lines.values_list('pk', 'quantity', 'nmck_unit', 'material_unit', 'application_unit', 'logistics_unit', 'comment'))
        plan = collect_plan()
        with tempfile.TemporaryDirectory() as directory, self.settings(BASE_DIR=directory), patch.dict('os.environ', {'REFRESH_ESTIMATES_MODE': 'apply', 'REFRESH_ESTIMATES_DIGEST': digest(plan)}), redirect_stdout(io.StringIO()):
            main()
        self.assertEqual(before, list(estimate.lines.values_list('pk', 'quantity', 'nmck_unit', 'material_unit', 'application_unit', 'logistics_unit', 'comment')))
        self.assertTrue(estimate.lines.filter(name='Ручка').exists())
        self.assertFalse(collect_plan()['updates'])

    def test_operator_skips_costed_or_component_replacements(self):
        from scripts.sync_test_estimate_requirements import collect_plan
        estimate = self.create()
        parent = self.parents[0]
        self.stage([self.row(parent, relationship='enrich', quantity=100, name='Ручка')])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        line = estimate.lines.first(); line.material_unit = 10; line.save(update_fields=['material_unit'])
        plan = collect_plan()
        self.assertFalse(plan['updates'])
        self.assertEqual(plan['skipped'][0]['reason'], 'existing_cost_or_route')
