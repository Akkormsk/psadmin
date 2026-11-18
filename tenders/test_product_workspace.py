from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .product_workspace import estimate_workspace


class EstimateWorkspaceTests(SimpleTestCase):
    def test_missing_or_inaccessible_estimate_does_not_expose_data(self):
        user = SimpleNamespace(pk=2, is_superuser=False)
        self.assertIsNone(estimate_workspace(None, user))
        estimate = SimpleNamespace(owner_id=3, lines=Mock())
        self.assertIsNone(estimate_workspace(estimate, user))
        estimate.lines.all.assert_not_called()

    def test_saved_line_keeps_source_identity_and_all_estimate_fields(self):
        line = SimpleNamespace(pk=10, group_key='set', group_name='Набор', name='Блокнот',
            quantity=100, nmck_unit=250, material_unit=20, application_unit=0, logistics_unit=0,
            product_url='', comment='Сохранённая заметка',
            requirements={'source_item_id': 42, 'production': {'session_id': 8}})
        estimate = SimpleNamespace(pk=5, owner_id=2, status='finalized', name='Расчёт',
            tender_number='001', reduction_percent=10, russia_delivery=50, result_notes='Итог',
            document_analysis={'saved': True}, lines=SimpleNamespace(all=lambda: [line]))
        result = estimate_workspace(estimate, SimpleNamespace(pk=2, is_superuser=True))
        self.assertEqual(result['lines'][0]['requirements']['source_item_id'], 42)
        self.assertEqual(result['lines'][0]['requirements']['production']['session_id'], 8)
        self.assertEqual(result['fields']['result_notes'], 'Итог')
        self.assertEqual(result['lines'][0]['comment'], 'Сохранённая заметка')
        self.assertFalse(result['editable'])
        self.assertIn('/5/', result['save_url'])

    @patch('tenders.input_freshness.estimate_input_status', return_value={'state': 'stale', 'lines': []})
    def test_changed_requirements_disable_pricing_even_for_a_draft(self, freshness):
        estimate = SimpleNamespace(pk=5, owner_id=2, status='draft', name='Расчёт',
            tender_number='001', reduction_percent=10, russia_delivery=0, result_notes='',
            document_analysis={}, lines=SimpleNamespace(all=lambda: []))
        result = estimate_workspace(estimate, SimpleNamespace(pk=2, is_superuser=True))
        self.assertFalse(result['editable'])
        self.assertEqual(result['input_status']['state'], 'stale')
