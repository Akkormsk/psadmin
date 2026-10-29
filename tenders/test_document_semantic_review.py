from copy import deepcopy
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from .document_enrichment import merge_item_requirements, characteristic_display
from . import test_document_components as fixtures
from .models import TenderSourceItem
from tender_selection.services import decide_all_document_proposals


class SemanticReviewTests(SimpleTestCase):
    def setUp(self):
        self.base = {'characteristics': [{'name': 'Упаковка', 'value': 'пакет', 'source': 'notification'}, {'name': 'Материал', 'value': 'металл', 'source': 'notification'}]}

    def proposal(self, first):
        return {'reconciled': True, 'characteristics': [first, {'resolution': 'keep', 'baseline_refs': [1]}]}

    def test_compatible_statements_form_one_value_and_preserve_original_evidence(self):
        before = deepcopy(self.base)
        result = merge_item_requirements(self.base, self.proposal({'name': 'Упаковка', 'value': 'индивидуальный пакет', 'document_value': 'индивидуальная упаковка каждого экземпляра', 'resolution': 'merge', 'baseline_refs': [0], 'reason': 'Общее требование уточняет конкретную упаковку'}))
        self.assertEqual(self.base, before)
        self.assertEqual(len(result['characteristics']), 2)
        fact = result['characteristics'][0]
        self.assertEqual([row['value'] for row in fact['source_facts']], ['пакет', 'индивидуальная упаковка каждого экземпляра'])
        parts = characteristic_display(fact)['parts']
        self.assertEqual(''.join(row['text'] for row in parts if row['added']), 'индивидуальный пакет')
        self.assertEqual(''.join(row['text'] for row in parts if not row['added']), '')
        self.assertEqual(merge_item_requirements(self.base, result), result)

    def test_missing_duplicate_and_unknown_fact_references_are_rejected(self):
        for refs in ([], [0, 0], [100], [True]):
            with self.assertRaises(ValueError):
                merge_item_requirements(self.base, self.proposal({'name': 'Упаковка', 'value': 'пакет', 'resolution': 'keep', 'baseline_refs': refs}))
        with self.assertRaises(ValueError):
            merge_item_requirements(self.base, {'reconciled': True, 'characteristics': [{'resolution': 'keep', 'baseline_refs': [0]}]})

    def test_keep_cannot_silently_change_an_existing_fact(self):
        with self.assertRaises(ValueError):
            merge_item_requirements(self.base, self.proposal({'name': 'Упаковка', 'value': 'коробка', 'resolution': 'keep', 'baseline_refs': [0]}))

    def test_conflict_preserves_both_values_and_stays_explicit(self):
        result = merge_item_requirements(self.base, {'reconciled': True, 'characteristics': [{'resolution': 'keep', 'baseline_refs': [0]}, {'name': 'Материал', 'value': 'металл / пластик', 'document_value': 'пластик', 'resolution': 'conflict', 'baseline_refs': [1], 'reason': 'Документы указывают разные материалы'}]})
        fact = result['characteristics'][1]
        self.assertTrue(fact['conflict'])
        self.assertEqual({row['value'] for row in fact['source_facts']}, {'металл', 'пластик'})

    def test_omission_requires_reason_and_keeps_the_original_fact(self):
        result = merge_item_requirements(self.base, self.proposal({'resolution': 'omit', 'baseline_refs': [0], 'reason': 'Условие вынесено в описание упаковки заказа'}))
        self.assertEqual(result['omitted_facts'][0]['source_facts'][0]['value'], 'пакет')
        with self.assertRaises(ValueError):
            merge_item_requirements(self.base, self.proposal({'resolution': 'omit', 'baseline_refs': [0]}))
        result['omitted_facts'] = []
        with self.assertRaises(ValueError):
            merge_item_requirements(self.base, result)

    def test_contradiction_between_new_document_facts_needs_no_fake_baseline_reference(self):
        result = merge_item_requirements({}, {"reconciled": True, "characteristics": [{"name": "Материал", "value": "металл / пластик", "resolution": "conflict", "baseline_refs": [], "reason": "Разные материалы в документах", "document_facts": [{"value": "металл"}, {"value": "пластик"}]}]})
        self.assertTrue(result["characteristics"][0]["conflict"])
        self.assertEqual({row["value"] for row in result["characteristics"][0]["source_facts"]}, {"металл", "пластик"})


class WholeProposalDecisionTests(TestCase):
    row = fixtures.DocumentComponentAcceptanceTests.row
    stage = fixtures.DocumentComponentAcceptanceTests.stage
    setUp = fixtures.DocumentComponentAcceptanceTests.setUp

    def test_one_atomic_decision_applies_the_entire_reviewed_snapshot(self):
        proposals = self.stage([self.row(parent) for parent in self.parents])['created']
        result = decide_all_document_proposals(self.admin, self.tender.pk, 'accept', [row.pk for row in proposals])
        self.assertEqual(result['changed'], 2)
        self.assertEqual(TenderSourceItem.objects.filter(pk__in=[row.pk for row in proposals], is_active=True, source_type='document_extraction').count(), 2)

    def test_later_invalid_group_rolls_back_earlier_group(self):
        first = self.stage([self.row(self.parents[0])])['created'][0]
        invalid = TenderSourceItem.objects.create(tender=self.tender, parent=self.parents[1], source_key='invalid', source_type='document_proposal', is_active=False, metadata={'relationship': 'component'})
        with self.assertRaises(ValueError):
            decide_all_document_proposals(self.admin, self.tender.pk, 'accept', [first.pk, invalid.pk])
        first.refresh_from_db(); self.parents[0].refresh_from_db()
        self.assertEqual(first.source_type, 'document_proposal')
        self.assertFalse(first.is_active)
        self.assertTrue(self.parents[0].is_active)

    def test_stale_review_snapshot_cannot_accept_new_unseen_proposals(self):
        proposals = self.stage([self.row(parent) for parent in self.parents])['created']
        with self.assertRaises(ValueError):
            decide_all_document_proposals(self.admin, self.tender.pk, 'accept', [proposals[0].pk])
        self.assertEqual(TenderSourceItem.objects.filter(source_type='document_proposal').count(), 2)

    def test_reject_all_and_repeat_do_not_change_originals(self):
        proposals = self.stage([self.row(parent) for parent in self.parents])['created']
        ids = [row.pk for row in proposals]
        decide_all_document_proposals(self.admin, self.tender.pk, 'reject', ids)
        self.assertFalse(TenderSourceItem.objects.filter(pk__in=ids).exists())
        self.assertEqual(TenderSourceItem.objects.filter(pk__in=[row.pk for row in self.parents], is_active=True).count(), 2)
        with self.assertRaises(ValueError):
            decide_all_document_proposals(self.admin, self.tender.pk, 'accept', ids)

    def test_card_has_one_pair_of_buttons_for_all_groups(self):
        proposals = self.stage([self.row(parent) for parent in self.parents])['created']
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]) + '?workspace=1')
        self.assertContains(response, 'Принять все правки', count=1)
        self.assertContains(response, 'Отклонить все', count=1)
        self.assertContains(response, 'ts-fact-added')
        url = reverse('tender_selection:all_document_proposals_action', args=[self.tender.pk]) + '?workspace=1'
        response = self.client.post(url, {'action': 'accept', 'proposal_ids': ','.join(str(row.pk) for row in proposals)})
        self.assertEqual(response['Location'], reverse('tender_selection:detail', args=[self.tender.pk]) + '?workspace=1#tender-items')

    def test_assistant_uses_same_whole_list_executor_and_requires_confirmation(self):
        from .assistant_tools import execute_tool, AssistantToolConfirmationRequired
        proposals = self.stage([self.row(parent) for parent in self.parents])['created']
        args = {'tender_id': self.tender.pk, 'decision': 'accept', 'proposal_ids': [row.pk for row in proposals]}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool('tender.document_proposals_all', self.admin, args)
        result = execute_tool('tender.document_proposals_all', self.admin, args, confirmed=True)
        self.assertEqual(result['changed'], 2)

    def test_semantic_failure_in_later_row_does_not_stage_a_partial_new_list(self):
        good = {'reconciled': True, 'characteristics': [{'resolution': 'new', 'baseline_refs': [], 'name': 'Упаковка', 'value': 'пакет', 'source_url': 'https://zakupki.gov.ru/components'}]}
        bad = {'reconciled': True, 'characteristics': [{'resolution': 'keep', 'baseline_refs': [999]}]}
        result = self.stage([self.row(self.parents[0], relationship='enrich', quantity=100, requirements=good), self.row(self.parents[1], relationship='enrich', quantity=50, requirements=bad)])
        self.assertEqual(result['state'], 'validation_failure')
        self.assertFalse(TenderSourceItem.objects.filter(source_type='document_proposal').exists())

    def test_new_model_enrichment_without_fact_coverage_is_rejected(self):
        from unittest.mock import patch
        from .calculation_v2_pipeline import GatewayDocumentEnricher, enrich_suspicious_tender, assess_quality
        raw = self.row(self.parents[0], relationship='enrich')
        raw['requirements'] = {'characteristics': [{'name': 'Упаковка', 'value': 'пакет'}]}
        with patch('tenders.services._ai_gateway_json', return_value=({'items': [raw]}, {})):
            result = enrich_suspicious_tender(self.tender, assess_quality(self.tender, self.parents), GatewayDocumentEnricher())
        self.assertEqual(result['state'], 'validation_failure')
        self.assertEqual(result['created'], [])

    def test_each_new_fact_must_cite_a_document_that_was_actually_supplied(self):
        from unittest.mock import patch
        from .calculation_v2_pipeline import GatewayDocumentEnricher, enrich_suspicious_tender, assess_quality
        raw = self.row(self.parents[0], relationship='enrich')
        raw['requirements'] = {'reconciled': True, 'characteristics': [{'name': 'Материал', 'value': 'металл', 'resolution': 'new', 'baseline_refs': [], 'document_facts': [{'value': 'металл', 'source_url': 'https://foreign.test/not-read'}]}]}
        with patch('tenders.services._ai_gateway_json', return_value=({'items': [raw]}, {})):
            result = enrich_suspicious_tender(self.tender, assess_quality(self.tender, self.parents), GatewayDocumentEnricher())
        self.assertEqual(result['state'], 'validation_failure')
        self.assertEqual(result['created'], [])
