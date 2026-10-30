from copy import deepcopy
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from .models import Tender
from .notification import parse_notification
from .national_regime import structured_item, document_item, resolve_item, risk_projection


class ItemEvidenceTests(SimpleTestCase):
    def raw(self, **flags):
        return {"restrictionsInfo": flags}

    def test_structured_red_yellow_green_and_unknown_with_exact_field_basis(self):
        cases = [(dict(isProhibitionForeignPurchaseObjects="true"), "red"),
                 (dict(isRestrictForeignPurchaseObjects="true"), "yellow"),
                 (dict(isProhibitionForeignPurchaseObjects="false", isRestrictForeignPurchaseObjects="false", isPreferenseRFPurchaseObjects="true"), "green"),
                 (dict(isPreferenseRFPurchaseObjects="true"), "green"),
                 ({}, "gray")]
        for flags, color in cases:
            result = structured_item(self.raw(**flags), "$.position[2]")
            self.assertEqual(result["color"], color)
            if color != "gray":
                self.assertTrue(result["basis"])
                self.assertTrue(result["basis"][0]["path"].startswith("$.position[2].restrictionsInfo"))

    def test_absence_requires_all_explicit_flags_not_silence_or_organizational_benefit(self):
        self.assertEqual(structured_item(self.raw(isProhibitionForeignPurchaseObjects=False), "$")["status"], "unknown")
        flags = {key: False for key in ("isProhibitionForeignPurchaseObjects", "isRestrictForeignPurchaseObjects", "isPreferenseRFPurchaseObjects")}
        self.assertEqual(structured_item(self.raw(**flags), "$")["status"], "none")
        card = parse_notification({"source": {"notificationInfo": {"preferensesInfo": {"name": "Преимущество организациям инвалидов"}, "purchaseObjectsInfo": {"notDrugPurchaseObjectsInfo": {"purchaseObject": {"name": "Товар", "OKPD2": {"OKPDCode": "17.23"}}}}}}})
        self.assertEqual(card["items"][0]["national_regime"]["color"], "gray")

    def test_document_quote_must_be_literal_correctly_scoped_and_support_the_status(self):
        url = "https://zakupki.gov.ru/spec"
        quote = "Для товара Ручка по ПП 1875 установлен запрет на допуск иностранных товаров."
        evidence = {"status": "ban", "basis": [{"quote": quote, "source_url": url, "page_or_section": "раздел 4"}]}
        result = document_item(evidence, {url: {"text": quote}}, name="Ручка")
        self.assertEqual(result["color"], "red")
        self.assertEqual(result["basis"][0]["quote"], quote)
        self.assertEqual(result["basis"][0]["page_or_section"], "раздел 4")
        for docs, name in [({url: {"text": "Другой текст"}}, "Ручка"), ({url: {"text": quote}}, "Блокнот"), ({url: {"text": "", "image_indexes": [1]}}, "Ручка")]:
            self.assertEqual(document_item(evidence, docs, name=name)["color"], "gray")
        absent = "Для товара Ручка по ПП 1875 запрет отсутствует."
        self.assertEqual(document_item({"status": "ban", "basis": [{"quote": absent, "source_url": url}]}, {url: {"text": absent}}, name="Ручка")["color"], "gray")
        bad = deepcopy(evidence); bad["status"] = "none"
        self.assertEqual(document_item(bad, {url: {"text": quote}}, name="Ручка")["color"], "gray")

    def test_document_yellow_green_and_explicit_none(self):
        for status, text in [("restriction", "Для товара Ручка по ПП 1875 требуется реестровая запись для подтверждения происхождения."), ("advantage", "Для товара Ручка по ПП 1875 установлено преимущество российским товарам."), ("none", "Для товара Ручка ПП 1875: национальный режим не применяется.")]:
            result = document_item({"status": status, "basis": [{"quote": text, "source_url": "doc"}]}, {"doc": {"text": text}}, name="Ручка")
            self.assertEqual(result["status"], status)

    def test_parent_requirement_not_blindly_inherited_by_components_and_conflict_is_gray(self):
        native = structured_item(self.raw(isProhibitionForeignPurchaseObjects=True), "$")
        self.assertEqual(resolve_item(native, component=True)["color"], "gray")
        from .national_regime import annotation
        self.assertEqual(resolve_item(native, annotation("none", [{"quote": "Нет требований"}]))["color"], "gray")


class ItemRiskTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="synthetic-1875", review=Tender.INTERESTING, notification_raw={"source": {"notificationInfo": {"purchaseObjectsInfo": {"notDrugPurchaseObjectsInfo": {"purchaseObject": [{"name": "Ручка", "restrictionsInfo": {"isProhibitionForeignPurchaseObjects": "true"}}, {"name": "Блокнот", "restrictionsInfo": {"isRestrictForeignPurchaseObjects": "true"}}, {"name": "Упаковка"}]}}}}})

    def test_risk_names_products_keeps_evidence_and_does_not_rewrite_manual_decisions(self):
        original = {"risk_level": "low", "risk_factors": [], "risk_facts": {"documents_sufficient": True}}
        self.tender.risk_assessment = original; self.tender.save()
        review = self.tender.review
        result = risk_projection(self.tender, original)
        self.assertEqual(result["risk_level"], "high")
        self.assertEqual({factor["code"] for factor in result["risk_factors"]}, {"item_1875_ban", "item_1875_confirmation", "item_1875_unknown"})
        ban = next(factor for factor in result["risk_factors"] if factor["code"] == "item_1875_ban")
        self.assertIn("Ручка", ban["text"])
        self.assertTrue(ban["items"][0]["regime"]["basis"])
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.review, review)
        self.assertEqual(self.tender.risk_assessment, original)

    def test_unbacked_generic_ai_ban_is_not_a_high_risk_and_unknown_is_not_green(self):
        self.tender.notification_raw = {"source": {"notificationInfo": {"purchaseObjectsInfo": {"notDrugPurchaseObjectsInfo": {"purchaseObject": {"name": "Ручка"}}}}}}
        result = risk_projection(self.tender, {"risk_level": "high", "risk_facts": {"documents_sufficient": True}, "risk_factors": [{"code": "national_regime", "level": "high", "text": "Догадка по ОКПД2"}]})
        self.assertEqual(result["risk_level"], "unknown")
        self.assertFalse(any(factor["level"] in {"high", "medium"} for factor in result["risk_factors"]))


class NationalReviewTests(TestCase):
    from tenders.test_document_components import DocumentComponentAcceptanceTests as Fixtures
    setUp = Fixtures.setUp
    row = Fixtures.row
    stage = Fixtures.stage

    def test_document_requirement_only_affects_accepted_item_and_rejection_preserves_decision(self):
        from tenders.models import TenderSourceItem
        from .models import DocumentPreview
        from .services import decide_document_proposals
        from .national_regime import source_item_status
        parent = self.parents[0]
        quote = "Для товара Ручка по ПП 1875 установлен запрет на допуск иностранных товаров."
        DocumentPreview.objects.filter(url="https://zakupki.gov.ru/components").update(html="<p>" + quote + "</p>")
        row = self.row(parent, relationship="enrich", name="Ручка", quantity=parent.quantity, national_regime={"status": "ban", "basis": [{"quote": quote, "source_url": "https://zakupki.gov.ru/components", "page_or_section": "раздел 5"}]})
        proposal = self.stage([row])["created"][0]
        self.assertEqual(proposal.metadata["national_regime"]["status"], "ban")
        self.assertEqual(source_item_status(parent)["status"], "unknown")
        initial_review = self.tender.review
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, "reject")
        parent.refresh_from_db(); self.tender.refresh_from_db()
        self.assertTrue(parent.is_active)
        self.assertEqual(self.tender.review, initial_review)
        self.assertFalse(TenderSourceItem.objects.filter(pk=proposal.pk).exists())
        proposal = self.stage([row])["created"][0]
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, "accept")
        proposal.refresh_from_db()
        self.assertEqual(source_item_status(proposal)["color"], "red")
        self.assertEqual(source_item_status(proposal)["basis"][0]["quote"], quote)

    def test_complete_response_wrappers_keep_validation_and_use_one_gateway_call(self):
        from tenders.calculation_v2_pipeline import GatewayDocumentEnricher, assess_quality
        row = self.row(self.parents[0], relationship="enrich", quantity=self.parents[0].quantity)
        for raw in ([row], row, {"items": row}):
            enricher = GatewayDocumentEnricher()
            with patch("tenders.services._ai_gateway_json", return_value=(raw, {"prompt_tokens": 100, "completion_tokens": 100})) as gateway:
                output = enricher.extract_tender(tender=self.tender, source_items=self.parents, documents=assess_quality(self.tender, self.parents)["documents"])
            self.assertEqual(len(output), 1)
            self.assertEqual(gateway.call_count, 1)
            self.assertIn("response_contract", gateway.call_args.kwargs)
        self.assertIn("1875: требуется проверка", self.parents[0].national_regime())

    def test_card_renders_separate_item_statuses_and_exact_structured_evidence(self):
        from django.urls import reverse
        from django.utils import timezone
        from tenders.calculation_v2_pipeline import ingest_source_items
        self.tender.v2_source_items.all().delete()
        rows = self.payload["source"]["notificationInfo"]["purchaseObjectsInfo"]["notDrugPurchaseObjectsInfo"]["purchaseObject"]
        rows[0]["restrictionsInfo"] = {"isProhibitionForeignPurchaseObjects": "true"}
        rows[1]["restrictionsInfo"] = {"isRestrictForeignPurchaseObjects": "true"}
        self.tender.notification_raw = self.payload
        self.tender.risk_assessment = {"risk_factors": [], "risk_facts": {"documents_sufficient": True}}
        self.tender.risk_checked_at = timezone.now()
        self.tender.save()
        ingest_source_items(self.tender)
        response = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]), {"workspace": 1}, secure=True)
        self.assertContains(response, "ts-national-pill--red")
        self.assertContains(response, "ts-national-pill--yellow")
        self.assertContains(response, "Запрет на иностранные товары: да")
        self.assertNotContains(response, "isProhibitionForeignPurchaseObjects")
        self.assertNotContains(response, "$.source.notificationInfo")
        self.assertContains(response, "Товары и основания")

    def test_assistant_read_uses_same_item_evidence_service(self):
        from tenders.assistant_tools import execute_tool
        result = execute_tool("read", self.admin, {"plan": {"entity": "tenders.TenderSourceItem", "fields": ["national_regime"], "filters": [{"field": "id", "op": "eq", "value": self.parents[0].pk}]}})
        self.assertEqual(result["records"][0]["national_regime"], self.parents[0].national_regime())

    def test_invalid_response_shape_is_reported_without_partial_changes(self):
        from tenders.calculation_v2_pipeline import GatewayDocumentEnricher, assess_quality
        for raw in ({"error": "no result"}, {"items": 42}, {"items": None}):
            enricher = GatewayDocumentEnricher()
            with patch("tenders.services._ai_gateway_json", return_value=(raw, {"prompt_tokens": 100, "completion_tokens": 100})):
                result = enricher.extract_tender(tender=self.tender, source_items=self.parents, documents=assess_quality(self.tender, self.parents)["documents"])
            self.assertEqual(result, [])
            self.assertEqual(enricher.diagnostics["outcome"], "validation_failure")
