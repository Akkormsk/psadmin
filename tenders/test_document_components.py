from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tender_selection.models import DocumentPreview, Tender
from tender_selection.services import decide_document_proposals
from .calculation_v2_pipeline import GatewayDocumentEnricher, assess_quality, enrich_suspicious_tender, ingest_source_items
from .document_enrichment import component_total_quantity
from .models import TenderCommercialItem, TenderSourceItem


class DocumentComponentAcceptanceTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("component-owner", password="test")
        self.client.force_login(self.admin)
        self.payload = {"source": {
            "notificationInfo": {"purchaseObjectsInfo": {"notDrugPurchaseObjectsInfo": {"purchaseObject": [
                {"name": "Общая позиция", "quantity": {"value": str(quantity)}, "OKEI": {"nationalCode": "шт"}, "price": str(price), "sum": str(quantity * price)}
                for quantity, price in [(100, 1000), (50, 2000)]
            ]}}},
            "attachmentsInfo": {"attachmentInfo": [{"url": "https://zakupki.gov.ru/components", "fileName": "Состав.docx"}]},
        }}
        self.tender = Tender.objects.create(purchase_number="component-example", title="Две исходные позиции", notification_raw=self.payload, extras_checked_at=timezone.now())
        self.parents = ingest_source_items(self.tender)
        DocumentPreview.objects.create(url="https://zakupki.gov.ru/components", kind="docx", html="<p>В позиции А два изделия; в позиции Б половина единицы материала.</p>")

    def row(self, parent, per_parent="2", **changes):
        row = {"parent_source_item_id": parent.pk, "parent_display_name": "Состав " + str(parent.metadata["notification_index"] + 1),
               "name": "Изделие", "quantity": 999, "quantity_per_parent": per_parent, "unit": "шт",
               "requirements": {"characteristics": [{"name": "Цвет", "value": "Синий"}]}, "confidence": .95,
               "source_url": "https://zakupki.gov.ru/components", "page_or_section": "Таблица", "evidence": "Количество в составе", "relationship": "component"}
        row.update(changes)
        if row["relationship"] == "enrich" and not row["requirements"].get("reconciled"):
            from .document_enrichment import effective_item_requirements
            baseline = effective_item_requirements(parent).get("characteristics", [])
            additions = row["requirements"].get("characteristics", [])
            row["requirements"] = {"reconciled": True, "characteristics": [{"resolution": "keep", "baseline_refs": [index]} for index in range(len(baseline))] + [{**fact, "resolution": "new", "baseline_refs": []} for fact in additions]}
        if row["relationship"] == "enrich":
            for fact in row["requirements"].get("characteristics", []):
                if isinstance(fact, dict):
                    fact.setdefault("source_url", row["source_url"])
        return row

    def stage(self, rows):
        with patch("tenders.services._ai_gateway_json", return_value=({"items": rows}, {"prompt_tokens": 100, "completion_tokens": 100})):
            return enrich_suspicious_tender(self.tender, assess_quality(self.tender, self.parents), GatewayDocumentEnricher())

    def test_quantities_are_computed_and_components_keep_their_own_parent(self):
        result = self.stage([self.row(self.parents[0]), self.row(self.parents[1], "0.5")])
        self.assertEqual([item.quantity for item in result["created"]], [Decimal("200"), Decimal("25")])
        self.assertEqual([item.parent_id for item in result["created"]], [item.pk for item in self.parents])
        self.assertTrue(all(not item.is_active for item in result["created"]))
        response = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]), {"workspace": 1})
        self.assertContains(response, "В одном")
        self.assertContains(response, "Всего")
        self.assertContains(response, "Состав 1")
        self.assertContains(response, "data-doc-preview=\"0\"")

    def test_accept_updates_card_even_when_old_commercial_projection_exists(self):
        old = TenderCommercialItem.objects.create(tender=self.tender, source_key="old-commercial", display_name="Старое представление", quantity=100)
        old.source_items.add(self.parents[0])
        proposal = self.stage([self.row(self.parents[0])])["created"][0]
        # Recheck at acceptance, rather than trusting a stale or model-supplied total.
        proposal.quantity = 999
        proposal.save(update_fields=["quantity"])
        url = reverse("tender_selection:document_proposal_action", args=[self.tender.pk, self.parents[0].pk]) + "?workspace=1"
        response = self.client.post(url, {"action": "accept"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("?workspace=1#tender-items"))
        proposal.refresh_from_db()
        self.assertEqual(proposal.quantity, Decimal("200"))
        self.assertTrue(proposal.is_active)
        self.assertEqual(proposal.metadata["quantity_per_parent"], "2")
        self.parents[1].refresh_from_db()
        self.assertTrue(self.parents[1].is_active)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.notification_raw, self.payload)
        card = self.client.get(response["Location"])
        found = next(item for item in card.context["display_items"] if item["name"] == "Изделие")
        self.assertEqual(found["quantity"], Decimal("200"))
        self.assertIsNone(found["price"])
        self.assertEqual(found["parent_display_name"], "Состав 1")
        self.assertContains(card, "Из документации")
        old.refresh_from_db()
        self.assertEqual(old.display_name, "Старое представление")

    def test_missing_component_amount_is_not_assumed_to_be_one(self):
        result = self.stage([self.row(self.parents[0], None)])
        self.assertEqual(result["created"], [])
        self.assertEqual(result["state"], "validation_failure")

    def test_aggregate_and_components_cannot_both_replace_the_same_parent(self):
        result = self.stage([self.row(self.parents[0]), self.row(self.parents[0], relationship="enrich")])
        self.assertEqual(result["created"], [])
        self.assertEqual(result["state"], "validation_failure")

    def test_accept_invalid_component_keeps_everything_unmodified(self):
        proposal = TenderSourceItem.objects.create(tender=self.tender, source_key="bad-component", source_type="document_proposal", parent=self.parents[0], is_active=False, quantity=10, metadata={"relationship": "component"})
        with self.assertRaises(ValueError):
            decide_document_proposals(self.admin, self.tender.pk, self.parents[0].pk, "accept")
        self.parents[0].refresh_from_db(); proposal.refresh_from_db()
        self.assertTrue(self.parents[0].is_active)
        self.assertFalse(proposal.is_active)

    def test_reject_does_not_change_notification_or_other_parents(self):
        proposal = self.stage([self.row(self.parents[0])])["created"][0]
        decide_document_proposals(self.admin, self.tender.pk, self.parents[0].pk, "reject")
        self.assertFalse(TenderSourceItem.objects.filter(pk=proposal.pk).exists())
        self.parents[0].refresh_from_db()
        self.assertTrue(self.parents[0].is_active)

    def test_precision_and_nonfinite_quantities_are_not_silently_rounded(self):
        for per_parent in ["NaN", "Infinity", "-1", "0", "0.001"]:
            with self.assertRaises(ValueError):
                component_total_quantity(Decimal("1"), per_parent)

    def test_assistant_uses_the_same_quantity_executor(self):
        from tenders.assistant_tools import execute_tool
        proposal = self.stage([self.row(self.parents[0])])["created"][0]
        result = execute_tool("tender.document_proposals", self.admin, {"tender_id": self.tender.pk, "parent_id": self.parents[0].pk, "decision": "accept"}, confirmed=True)
        self.assertEqual(result["changed"], 1)
        proposal.refresh_from_db()
        self.assertEqual(proposal.quantity, Decimal("200"))
        self.assertTrue(proposal.is_active)

    def test_invalid_button_action_cannot_silently_reject_proposals(self):
        proposal = self.stage([self.row(self.parents[0])])["created"][0]
        self.client.post(reverse("tender_selection:document_proposal_action", args=[self.tender.pk, self.parents[0].pk]), {"action": "invalid"})
        self.assertTrue(TenderSourceItem.objects.filter(pk=proposal.pk).exists())

    def test_missing_top_level_items_is_failure_not_empty_search(self):
        with patch("tenders.services._ai_gateway_json", return_value=({"name": "Вложенная строка"}, {"prompt_tokens": 100, "completion_tokens": 100})):
            result = enrich_suspicious_tender(self.tender, assess_quality(self.tender, self.parents), GatewayDocumentEnricher())
        self.assertEqual(result["state"], "validation_failure")
        self.assertEqual(result["created"], [])

    def test_truncated_gateway_response_is_not_salvaged_as_inner_json(self):
        import io
        import json
        from .services import TenderAIError, _ai_gateway_json
        response = {"choices": [{"finish_reason": "length", "message": {"content": '{"items":[{"name":"Вложенная строка"},'}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 8000}}
        with patch.dict("os.environ", {"TIMEWEB_AI_API_KEY": "synthetic-test-key"}), patch("tenders.services.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as request:
            with self.assertRaises(TenderAIError) as error:
                _ai_gateway_json("synthetic", network_attempts=1, json_attempts=1)
        self.assertIn("лимит вывода", str(error.exception))
        self.assertEqual(error.exception.usage["completion_tokens"], 8000)
        self.assertEqual(request.call_count, 1)
