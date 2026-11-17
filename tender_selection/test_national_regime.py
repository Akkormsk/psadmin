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
        self.assertEqual({factor["code"] for factor in result["risk_factors"]}, {"item_1875_ban", "item_1875_confirmation"})
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

    def test_checked_absence_is_one_quiet_summary_without_unknown_risk_factor(self):
        from tenders.models import TenderComputeJob
        from .services import _document_input_fingerprint
        self.tender.notification_raw = {'source': {'notificationInfo': {'purchaseObjectsInfo': {
            'notDrugPurchaseObjectsInfo': {'purchaseObject': [{'name': 'Блокнот'}, {'name': 'Ручка'}]}}}}}
        self.tender.save(update_fields=['notification_raw'])
        self.tender.notification_raw['source']['attachmentsInfo'] = {'attachmentInfo': [{'url': 'https://zakupki.gov.ru/spec', 'fileName': 'ТЗ.docx'}]}
        self.tender.save(update_fields=['notification_raw'])
        job = TenderComputeJob.objects.create(tender=self.tender, version='v2-intake', status='needs_review', diagnostics={
            'document_input_fingerprint': _document_input_fingerprint(self.tender),
            'stages': {'documents': {'state': 'complete'}, 'national_regime': {'state': 'complete'}},
        })
        result = risk_projection(self.tender, {'risk_facts': {'documents_sufficient': True}})
        self.assertTrue(result['national_regime_checked'])
        self.assertEqual(result['national_regime'], 'Требования по ПП №1875 в проверенных материалах не указаны.')
        self.assertFalse(result['risk_factors'])
        from django.template.loader import render_to_string
        rendered = render_to_string('tender_selection/_risk_block.html', {'risk': result})
        self.assertNotIn('По товарам и источникам', rendered)
        self.assertNotIn('требует проверки', rendered)
        self.assertEqual(result['risk_level'], 'low')
        self.assertTrue(all(item['regime']['status'] == 'unknown' for item in result['risk_facts']['national_regime_items']))
        stale_risk = risk_projection(self.tender, {'stale': True, 'risk_facts': {'documents_sufficient': True}})
        self.assertTrue(stale_risk['stale'])
        self.assertTrue(stale_risk['national_regime_checked'])
        self.assertNotIn('Проверка документов не завершена', stale_risk['national_regime'])
        job.diagnostics['source_reading'] = {'read': 2, 'total': 2, 'complete': True}
        job.save(update_fields=['diagnostics'])
        self.assertTrue(risk_projection(self.tender, {'stale': True})['national_regime_checked'])
        job.diagnostics['source_reading'] = {'read': 1, 'total': 2, 'complete': False}
        job.save(update_fields=['diagnostics'])
        self.assertFalse(risk_projection(self.tender)['national_regime_checked'])
        job.diagnostics.pop('source_reading')
        job.diagnostics['analysis_gaps'] = [{'stage': 'documents', 'reason': 'Файл недоступен'}]
        job.save(update_fields=['diagnostics'])
        incomplete = risk_projection(self.tender, {'risk_facts': {'documents_sufficient': True}})
        self.assertFalse(incomplete['national_regime_checked'])
        self.assertIn('Проверка документов не завершена', incomplete['national_regime'])
        self.assertEqual(incomplete['risk_level'], 'unknown')
        job.diagnostics.pop('analysis_gaps')
        job.diagnostics['document_input_fingerprint'] = 'old-inputs'
        job.save(update_fields=['diagnostics'])
        self.assertFalse(risk_projection(self.tender)['national_regime_checked'])

    def test_unknown_item_has_no_repeated_expander_but_conflicting_evidence_remains_visible(self):
        from django.template.loader import render_to_string
        from .national_regime import annotation
        quiet = render_to_string('tender_selection/_1875.html', {'regime': annotation()})
        self.assertNotIn('<details', quiet)
        self.assertIn('1875: не указано', quiet)
        conflict = render_to_string('tender_selection/_1875.html', {'regime': annotation(basis=[{'quote': 'Источники расходятся'}])})
        self.assertIn('<details', conflict)

    def test_import_reuses_notice_and_keeps_previous_item_evidence(self):
        from tenders.calculation_v2_pipeline import ingest_source_items
        from .national_regime import source_item_status, tender_items
        sources = ingest_source_items(self.tender)
        self.assertEqual([row.pk for row in ingest_source_items(self.tender)], [row.pk for row in sources])
        pen = sources[0]
        raw = deepcopy(self.tender.notification_raw)
        raw['source']['notificationInfo']['purchaseObjectsInfo']['notDrugPurchaseObjectsInfo']['purchaseObject'][0]['restrictionsInfo'] = {'isRestrictForeignPurchaseObjects': True}
        self.tender.notification_raw = raw
        self.tender.save(update_fields=['notification_raw'])
        # A different current notice must not rewrite the saved position's origin.
        self.assertEqual(source_item_status(pen)['status'], 'ban')
        self.assertEqual(tender_items(self.tender)[0]['regime']['status'], 'ban')
        updated = ingest_source_items(self.tender)
        self.assertNotEqual(updated[0].pk, pen.pk)
        self.assertEqual(updated[0].supersedes_id, pen.pk)
        self.assertEqual(source_item_status(updated[0])['status'], 'restriction')
        self.assertEqual(source_item_status(pen)['status'], 'ban')

    def test_known_risk_without_documents_does_not_call_ai(self):
        from .services import risk_assessment_for
        with patch('tender_selection.risk_assessment.assess') as ai:
            result = risk_assessment_for(self.tender)
        ai.assert_not_called()
        self.assertEqual(result['risk_level'], 'high')
        self.assertTrue(result['preliminary'])
        self.assertEqual(result['risk_facts']['national_regime_items'][0]['regime']['status'], 'ban')
        self.assertNotEqual(result['risk_facts'].get('documents_sufficient'), True)
        from django.template.loader import render_to_string
        html = render_to_string('tender_selection/_risk_block.html', {'risk': result, 'risk_error': self.tender.risk_error})
        self.assertIn('Ручка', html)
        self.assertIn('Повторить проверку документов', html)
        self.assertIn('Для полной оценки нужны доступные документы', html)

    def test_existing_position_gains_saved_evidence_without_replacing_accepted_changes(self):
        from tenders.calculation_v2_pipeline import ingest_source_items, _fingerprint, _notification_rows
        from tenders.models import TenderSourceItem
        from .national_regime import source_item_status
        source = ingest_source_items(self.tender)[0]
        row = _notification_rows(self.tender)[0]
        source.metadata.pop('notification_national_regime')
        source.metadata['source_fingerprint'] = _fingerprint({'name': row['name'], 'quantity': row['quantity'],
            'unit': row['unit'], 'requirements': {'characteristics': row.get('characteristics') or []}, 'code': row.get('code') or ''})
        source.is_active = False
        source.save(update_fields=['metadata', 'is_active'])
        accepted = TenderSourceItem.objects.create(tender=self.tender, source_key='accepted-pen',
            source_type='document_enrichment', parent=source, original_text='Ручка синяя', metadata={'relationship': 'enrich'})
        self.assertEqual(ingest_source_items(self.tender)[0].pk, source.pk)
        source.refresh_from_db()
        accepted.refresh_from_db()
        self.assertFalse(source.is_active)
        self.assertTrue(accepted.is_active)
        self.assertEqual(source_item_status(accepted)['status'], 'ban')

    def test_known_risk_survives_document_and_ai_failure(self):
        from .services import risk_assessment_for
        from .risk_assessment import RiskAssessmentError
        with patch('tender_selection.risk_assessment.select_documents', return_value=[{'name': 'Проект контракта', 'url': 'https://zakupki.gov.ru/contract'}]), patch('tender_selection.services.documents_html', return_value={}), patch('tender_selection.risk_assessment.assess', side_effect=RiskAssessmentError('Недоступно')) as ai:
            result = risk_assessment_for(self.tender)
        ai.assert_not_called()
        self.assertEqual(result['risk_level'], 'high')
        self.assertTrue(result['preliminary'])

    def test_ai_error_does_not_hide_saved_facts_or_replace_previous_assessment(self):
        from .services import risk_assessment_for
        from .risk_assessment import RiskAssessmentError
        previous = {'risk_factors': [{'code': 'short_deadline', 'level': 'high', 'text': 'Короткий срок'}], 'risk_facts': {'documents_sufficient': True}}
        self.tender.risk_assessment = previous
        self.tender.save(update_fields=['risk_assessment'])
        with patch('tender_selection.risk_assessment.select_documents', return_value=[{'name': 'Проект контракта', 'url': 'https://zakupki.gov.ru/contract'}]), patch('tender_selection.services.documents_html', return_value={'https://zakupki.gov.ru/contract': '<p>Срок исполнения десять дней</p>'}), patch('tender_selection.risk_assessment.assess', side_effect=RiskAssessmentError('Недоступно')) as ai:
            result = risk_assessment_for(self.tender, force=True)
        ai.assert_called_once()
        self.assertIn('item_1875_ban', {factor['code'] for factor in result['risk_factors']})
        self.assertIn('short_deadline', {factor['code'] for factor in result['risk_factors']})
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.risk_assessment, previous)

    def test_all_saved_states_are_shared_with_assistant_without_current_notice(self):
        from tenders.calculation_v2_pipeline import ingest_source_items
        from .national_regime import source_item_status, tender_items, FIELDS
        rows = self.tender.notification_raw['source']['notificationInfo']['purchaseObjectsInfo']['notDrugPurchaseObjectsInfo']['purchaseObject']
        rows.extend([{'name': 'Открытка', 'restrictionsInfo': {'isPreferenseRFPurchaseObjects': True}}, {'name': 'Коробка', 'restrictionsInfo': dict.fromkeys(FIELDS, False)}])
        self.tender.save(update_fields=['notification_raw'])
        sources = ingest_source_items(self.tender)
        self.tender.notification_raw = {}
        self.tender.save(update_fields=['notification_raw'])
        expected = ['ban', 'restriction', 'unknown', 'advantage', 'none']
        self.assertEqual({row['source_id']: row['regime']['status'] for row in tender_items(self.tender)},
                         {source.pk: status for source, status in zip(sources, expected)})
        for source, status in zip(sources, expected):
            self.assertEqual(source_item_status(source)['status'], status)
            self.assertIn(source_item_status(source)['label'], source.national_regime())


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
        proposal.refresh_from_db()
        self.assertEqual(proposal.source_type, 'document_rejected')
        self.assertFalse(proposal.is_active)
        proposal = self.stage([row])["created"][0]
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, "accept")
        proposal.refresh_from_db()
        self.assertEqual(source_item_status(proposal)["color"], "red")
        self.assertEqual(source_item_status(proposal)["basis"][0]["quote"], quote)
        self.assertEqual([row['decision'] for row in proposal.metadata['review_history']], ['reject', 'accept'])

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
        self.assertIn("1875: не указано", self.parents[0].national_regime())

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
        self.assertContains(response, "По товарам и источникам")

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
