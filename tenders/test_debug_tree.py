from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tender_selection.models import Tender
from tenders.models import TenderSourceItem, TenderCommercialItem


class TenderTreeDebugTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number='tree-inspect')
        self.source = TenderSourceItem.objects.create(
            tender=self.tender, source_key='1', original_text='Набор',
            quantity=2, unit='шт', requirements={'name': 'Подарочный набор'},
        )
        self.url = reverse('tender_tree_debug', args=[self.tender.pk])
        user = get_user_model()
        self.staff = user.objects.create_user(username='tree-staff', password='unused', is_staff=True)
        self.regular = user.objects.create_user(username='tree-reader', password='unused')

    def test_requires_login_and_staff(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.regular)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_sees_existing_source_without_creating_working_item(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Набор')
        self.assertContains(response, 'ТЗ и источники')
        self.assertContains(response, 'Без владельца')
        self.assertEqual(TenderCommercialItem.objects.filter(tender=self.tender).count(), 0)

    def test_only_get_is_allowed(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self.url).status_code, 405)
