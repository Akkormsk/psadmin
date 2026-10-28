from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from tenders.models import TenderComputeJob, TenderSourceItem
from .models import FilterSettings, Tender


class IncomingAnalysisStatusTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("status-ui", password="x")
        self.client.force_login(user)
        settings = FilterSettings.load()
        settings.min_price = 0
        settings.include_words = ""
        settings.exclude_words = ""
        settings.save()

    def test_list_receives_pre_resolved_presentation_status(self):
        tender = Tender.objects.create(purchase_number="status-ui-1", title="Поставка папок")
        TenderComputeJob.objects.create(tender=tender, version="v2", status=TenderComputeJob.Status.READY, diagnostics={"enrichment_state": "success", "enrichment_used": True})
        TenderSourceItem.objects.create(tender=tender, source_key="doc:status", source_type="document_extraction", original_text="Папка")
        response = self.client.get(reverse("tender_selection:list") + "?view=list", secure=True)
        self.assertEqual(response.status_code, 200)
        shown = next(item for item in response.context["page_obj"].object_list if item.pk == tender.pk)
        self.assertEqual(shown.analysis_status.code, "ENRICHED")
        self.assertEqual(shown.analysis_status.label, "Позиции уточнены по ТЗ")
        detail_url = reverse("tender_selection:detail", args=[tender.pk])
        self.assertContains(response, f'class="ts-row__main" href="{detail_url}"')
        self.assertContains(response, f'href="{detail_url}" data-workspace-open')

    def test_authorized_user_can_open_tender_detail(self):
        tender = Tender.objects.create(purchase_number="status-ui-detail", title="Открываемая карточка", source=Tender.MANUAL)
        response = self.client.get(reverse("tender_selection:detail", args=[tender.pk]) + "?workspace=1", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertContains(response, "Открываемая карточка")


class TenderDetailCompositionPresentationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("composition-ui", password="x")
        self.client.force_login(self.user)

    def test_working_list_keeps_unrelated_notification_item_and_shows_document_provenance(self):
        payload = {
            "source": {
                "commonInfo": {"purchaseObjectInfo": "Сувениры"},
                "notificationInfo": {"purchaseObjectsInfo": {"notDrugPurchaseObjectsInfo": {"purchaseObject": [
                    {"name": "Агрегированная позиция", "quantity": {"value": "100"}, "OKEI": {"nationalCode": "796"}},
                    {"name": "Независимая позиция", "quantity": {"value": "100"}, "OKEI": {"nationalCode": "796"}},
                ]}}},
                "attachmentsInfo": {"attachmentInfo": [{"fileName": "ТЗ", "url": "https://example.test/spec"}]},
            }
        }
        tender = Tender.objects.create(purchase_number="composition-ui-1", title="Сувениры", notification_raw=payload)
        parent = TenderSourceItem.objects.create(tender=tender, source_key="notification:aggregate", source_type="notification", original_text="Агрегированная позиция", quantity="100", unit="шт.", is_active=False)
        TenderSourceItem.objects.create(tender=tender, source_key="notification:independent", source_type="notification", original_text="Независимая позиция", quantity="100", unit="шт.")
        TenderSourceItem.objects.create(tender=tender, source_key="derived:alpha", source_type="document_extraction", original_text="Компонент Альфа", quantity="100", unit="шт.", parent=parent, requirements={"Параметр": "значение"}, provenance={"document_url": "https://example.test/spec", "page_or_section": "таблица 2", "evidence": "Компонент Альфа"})
        TenderSourceItem.objects.create(tender=tender, source_key="derived:beta", source_type="document_extraction", original_text="Компонент Бета", quantity="100", unit="шт.", parent=parent, requirements={"Свойство": "значение"}, provenance={"document_url": "https://example.test/spec", "page_or_section": "таблица 2"})

        with patch("tender_selection.views.notification_for", return_value=payload):
            response = self.client.get(reverse("tender_selection:detail", args=[tender.pk]), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Компонент Альфа")
        self.assertContains(response, "Компонент Бета")
        self.assertContains(response, "Независимая позиция")
        self.assertContains(response, "Из документации")
        self.assertContains(response, "Показать исходную запись")
        self.assertContains(response, "Агрегированная позиция")
        self.assertContains(response, "Компонент Альфа")
    def test_enrichment_badge_discloses_only_the_fields_added_from_document(self):
        payload = {"source": {"commonInfo": {"purchaseObjectInfo": "Папки"}, "notificationInfo": {"purchaseObjectsInfo": {"notDrugPurchaseObjectsInfo": {"purchaseObject": [{"name": "Папка", "quantity": {"value": "20"}, "OKEI": {"nationalCode": "796"}}]}}}, "attachmentsInfo": {"attachmentInfo": [{"fileName": "ТЗ", "url": "https://example.test/spec"}]}}}
        tender = Tender.objects.create(purchase_number="composition-ui-2", title="Папки", notification_raw=payload)
        original = TenderSourceItem.objects.create(tender=tender, source_key="notification:folder", source_type="notification", original_text="Папка", quantity="20", unit="шт.", is_active=False)
        TenderSourceItem.objects.create(tender=tender, source_key="enriched:folder", source_type="document_enrichment", original_text="Папка", quantity="20", unit="шт.", parent=original, requirements={"characteristics": [{"name": "Цвет", "value": "синий", "source": "notification"}, {"name": "Плотность", "value": "250 г/м²", "source": "document"}], "document_requirements": {"Плотность": "250 г/м²"}}, provenance={"document_url": "https://example.test/spec", "page_or_section": "таблица 1"})

        with patch("tender_selection.views.notification_for", return_value=payload):
            response = self.client.get(reverse("tender_selection:detail", args=[tender.pk]), secure=True)

        self.assertContains(response, "Уточнено по документации")
        self.assertContains(response, "Добавлено из ТЗ")
        self.assertContains(response, "Плотность")
        self.assertContains(response, "250 г/м²")
        self.assertContains(response, "Цвет")
        self.assertContains(response, "синий")

    def test_checked_no_changes_is_quiet_in_incoming_list(self):
        from tenders.models import TenderComputeJob
        tender = Tender.objects.create(purchase_number="quiet-status", title="Quiet")
        TenderComputeJob.objects.create(tender=tender, status=TenderComputeJob.Status.READY, diagnostics={"enrichment_state": "no_data"})
        self.client.force_login(get_user_model().objects.create_superuser("quiet-ui", password="x"))
        response = self.client.get(reverse("tender_selection:list"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ТЗ проверено")
