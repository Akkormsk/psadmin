import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tender_selection.models import Tender
from .models import OrderEstimate, TenderEstimate


class EstimateEntityBoundaryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="manager", password="test-pass")
        self.client.force_login(self.user)

    def _payload(self):
        return {
            "tender_number": "ORDER-1",
            "name": "Самостоятельный расчёт",
            "reduction_percent": "30",
            "russia_delivery": "0",
            "result_notes": "",
            "document_analysis_json": "{}",
            "lines_json": json.dumps([{
                "name": "Папка", "quantity": "10", "nmck_unit": "100",
                "material_unit": "20", "application_unit": "10", "logistics_unit": "5",
            }]),
        }

    def test_standalone_calculation_creates_order_without_tender(self):
        response = self.client.post(reverse("tender_estimate_create"), self._payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(OrderEstimate.objects.count(), 1)
        self.assertEqual(TenderEstimate.objects.count(), 0)
        self.assertEqual(Tender.objects.count(), 0)
        order = OrderEstimate.objects.get()
        self.assertEqual(order.lines.count(), 1)
        self.assertEqual(response.json()["url"], reverse("tender_estimate", args=[order.pk]))

    def test_legacy_calculation_id_redirects_to_migrated_order_without_tender_link(self):
        order = OrderEstimate.objects.create(
            owner=self.user, legacy_calculation_id=42, order_number="OLD-1", name="Старый",
        )
        response = self.client.get(reverse("tender_estimate_legacy", args=[42]))
        self.assertRedirects(response, reverse("tender_estimate", args=[order.pk]), fetch_redirect_response=False)
        self.assertEqual(Tender.objects.count(), 0)
        self.assertEqual(TenderEstimate.objects.count(), 0)

    def test_pipeline_calculation_remains_bound_to_its_tender(self):
        tender = Tender.objects.create(law="fz44", purchase_number="pipeline-1", object_info="Тестовый тендер")
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="pipeline-1", name="Заказчик")
        response = self.client.get(reverse("tender_pipeline_estimate", args=[estimate.pk]))
        self.assertContains(response, "Вернуться к тендеру")
        self.assertContains(response, "Исходный тендер")
