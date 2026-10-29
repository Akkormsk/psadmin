from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from .models import Tender
from .services import import_tender_by_number
from .gosplan import GosplanError, GosplanNotFoundError


class ImportByNumberTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('import-owner', password='test')
        self.number = '0123456789012345678'
        self.record = {'purchase_number': self.number, 'object_info': 'Поставка блокнотов', 'max_price': '5000', 'customers': ['1234567890']}
        self.client.force_login(self.user)

    def test_import_and_repeat_preserve_existing_lifecycle(self):
        with patch('tender_selection.gosplan.fetch_purchase', return_value=self.record) as fetch:
            result = import_tender_by_number(self.user, self.number)
            self.assertTrue(result['created'])
            tender = Tender.objects.get(pk=result['tender_id'])
            self.assertEqual(tender.title, 'Поставка блокнотов')
            tender.status = 'hidden'; tender.save(update_fields=['status'])
            repeated = import_tender_by_number(self.user, self.number)
            self.assertFalse(repeated['created'])
            fetch.assert_called_once_with(self.number)
            tender.refresh_from_db(); self.assertEqual(tender.status, 'hidden')

    def test_missing_invalid_and_network_errors_leave_no_empty_card(self):
        for record in (None, {}, {'purchase_number': '0000000000000000000', 'object_info': 'Чужая закупка'}, {'purchase_number': self.number}):
            with patch('tender_selection.gosplan.fetch_purchase', return_value=record), self.assertRaises(ValueError):
                import_tender_by_number(self.user, self.number)
        with patch('tender_selection.gosplan.fetch_purchase', side_effect=GosplanNotFoundError('404')), self.assertRaisesRegex(ValueError, 'не найдена'):
            import_tender_by_number(self.user, self.number)
        with patch('tender_selection.gosplan.fetch_purchase', side_effect=GosplanError('offline')), self.assertRaises(ValueError):
            import_tender_by_number(self.user, self.number)
        with patch('tender_selection.gosplan.fetch_purchase') as fetch, self.assertRaises(ValueError):
            import_tender_by_number(self.user, '123')
        fetch.assert_not_called()
        self.assertFalse(Tender.objects.exists())

    def test_http_json_and_permissions_and_template(self):
        url = reverse('tender_selection:import_by_number')
        with patch('tender_selection.gosplan.fetch_purchase', return_value=self.record):
            response = self.client.post(url, {'purchase_number': self.number}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['url'], reverse('tender_selection:detail', args=[response.json()['tender_id']]))
        response = self.client.get(reverse('tender_selection:list') + '?view=list')
        self.assertContains(response, 'id="ts-import-form"')
        self.assertContains(response, "status.textContent = 'Ищем…'")
        self.user.is_superuser = False; self.user.save(update_fields=['is_superuser'])
        self.assertEqual(self.client.post(url, {'purchase_number': self.number}).status_code, 403)
        with self.assertRaises(ValueError):
            import_tender_by_number(self.user, self.number)

    def test_assistant_requires_confirmation_and_uses_same_executor(self):
        from tenders.assistant_tools import execute_tool, AssistantToolConfirmationRequired
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool('tender.import_by_number', self.user, {'purchase_number': self.number})
        with patch('tender_selection.gosplan.fetch_purchase', return_value=self.record):
            result = execute_tool('tender.import_by_number', self.user, {'purchase_number': self.number}, confirmed=True)
        self.assertTrue(result['created'])
