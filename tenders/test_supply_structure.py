from django.test import TestCase
from django.urls import reverse
from tender_selection.services import decide_document_proposals
from . import test_document_components as fixtures
from .models import TenderSourceItem
from .calculation_v2_pipeline import active_calculation_items


class SupplyStructureTests(TestCase):
    setUp = fixtures.DocumentComponentAcceptanceTests.setUp
    row = fixtures.DocumentComponentAcceptanceTests.row
    stage = fixtures.DocumentComponentAcceptanceTests.stage

    def test_finished_product_with_completeness_keeps_one_item_and_unit_price(self):
        parent = self.parents[0]
        proposal = self.stage([self.row(parent, relationship='enrich', quantity=100, name='Готовое изделие', supply_kind='finished', requirements={'reconciled': True, 'characteristics': [{'resolution': 'new', 'baseline_refs': [], 'name': 'Комплектация', 'value': 'Изделие с принадлежностями и документами', 'scope': 'completeness', 'source_url': 'https://zakupki.gov.ru/components'}]})])['created'][0]
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        self.assertEqual(len(active_calculation_items(self.tender)), 2)
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        row = next(row for row in response.context['display_items'] if row['name'] == 'Готовое изделие')
        self.assertEqual(row['price'], '1000')
        self.assertEqual(row['original_item']['quantity'], '100')
        self.assertEqual(len(row['completeness']), 1)
        self.assertFalse(row['characteristics'])

    def test_component_requires_explicit_kit_classification_with_reason(self):
        for changes in ({'supply_kind': 'finished'}, {'supply_kind': 'unclear'}, {'supply_kind': 'kit', 'structure_reason': ''}):
            self.assertEqual(self.stage([self.row(self.parents[0], **changes)])['state'], 'validation_failure')

    def test_reanalysis_can_propose_restoring_one_product_without_mutating_accepted_children(self):
        parent = self.parents[0]
        children = self.stage([self.row(parent, name='Изделие'), self.row(parent, name='Принадлежность')])['created']
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        result = self.stage([self.row(parent, relationship='enrich', quantity=100, name='Изделие в комплекте', supply_kind='finished')])
        proposal = result['created'][0]
        self.assertEqual(set(proposal.metadata['replaces_source_ids']), {child.pk for child in children})
        self.assertEqual(len(active_calculation_items(self.tender)), 3)
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        self.assertEqual(len(active_calculation_items(self.tender)), 2)
        self.assertEqual(self.tender.v2_source_items.filter(pk__in=[child.pk for child in children], is_active=False).count(), 2)
        self.assertTrue(self.tender.v2_source_items.filter(pk=proposal.pk, is_active=True).exists())

    def test_changed_old_composition_rejects_restoration(self):
        parent = self.parents[0]
        children = self.stage([self.row(parent)])['created']
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        self.stage([self.row(parent, relationship='enrich', quantity=100, name='Изделие в комплекте')])
        children[0].is_active = False; children[0].save(update_fields=['is_active'])
        with self.assertRaises(ValueError):
            decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')

    def test_kit_is_open_by_default_with_original_group_finance_and_collapsed_item_tz(self):
        parent = self.parents[0]
        self.stage([self.row(parent, name='Первый товар'), self.row(parent, name='Второй товар')])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        self.assertEqual(len(response.context['display_item_groups']), 1)
        group = response.context['display_item_groups'][0]
        self.assertEqual(group['original']['sum'], '100000')
        self.assertEqual(len(group['items']), 2)
        self.assertContains(response, 'ts-item-group" open')
        self.assertContains(response, 'Цена группы не распределялась')
        self.assertContains(response, 'ТЗ ·')

    def test_rejected_restoration_keeps_previously_accepted_components(self):
        parent = self.parents[0]
        children = self.stage([self.row(parent), self.row(parent, name='Другое изделие')])['created']
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        self.stage([self.row(parent, relationship='enrich', quantity=100, name='Сохранить целиком', supply_kind='unclear')])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'reject')
        self.assertEqual(self.tender.v2_source_items.filter(pk__in=[child.pk for child in children], is_active=True).count(), 2)

    def test_ambiguous_whole_product_is_reviewed_without_splitting(self):
        parent = self.parents[0]
        self.stage([self.row(parent, relationship='enrich', quantity=100, name='Целая позиция', supply_kind='unclear', structure_reason='Документы не определяют самостоятельность составляющих')])
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        self.assertContains(response, 'Нужно проверить состав')
        self.assertEqual(len(active_calculation_items(self.tender)), 2)
        self.assertContains(response, 'Принять все правки', count=1)

    def test_price_comparison_retains_kopecks(self):
        from tender_selection.templatetags.tender_selection_extras import rub_precise
        self.assertIn('866,67', rub_precise('866.67'))
        self.assertEqual(rub_precise(None), '—')
        self.assertEqual(rub_precise('NaN'), '—')

    def test_two_accepted_kits_keep_separate_origins_prices_and_children(self):
        for index, parent in enumerate(self.parents):
            self.stage([self.row(parent, name=f'Товар {index}.А'), self.row(parent, name=f'Товар {index}.Б')])
            decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        groups = response.context['display_item_groups']
        self.assertEqual(len(groups), 2)
        self.assertEqual({group['original']['quantity'] for group in groups}, {'100', '50'})
        for group in groups:
            self.assertEqual(len(group['items']), 2)
            self.assertTrue(all(item['group_key'] == group['key'] for item in group['items']))
            self.assertTrue(all(item['original_item']['quantity'] == group['original']['quantity'] for item in group['items']))

    def test_same_call_receives_prior_composition_for_semantic_reassessment(self):
        from unittest.mock import patch
        from .calculation_v2_pipeline import GatewayDocumentEnricher, assess_quality, enrich_suspicious_tender
        parent = self.parents[0]
        self.stage([self.row(parent, name='Изделие'), self.row(parent, name='Принадлежность')])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept')
        raw = self.row(parent, relationship='enrich', quantity=100, name='Готовое изделие', supply_kind='finished')
        with patch('tenders.services._ai_gateway_json', return_value=({'items': [raw]}, {})) as gateway:
            result = enrich_suspicious_tender(self.tender, assess_quality(self.tender, self.parents), GatewayDocumentEnricher())
        self.assertEqual(gateway.call_count, 1)
        self.assertEqual(result['state'], 'needs_review')
        prompt = gateway.call_args.args[0]
        self.assertIn('existing_components', prompt)
        self.assertIn('when baseline_facts is empty', prompt)
        self.assertIn('Принадлежность', prompt)
        self.assertIn('supply_kind=finished|kit|unclear', prompt)

    def test_orphan_historical_proposal_stays_readable_without_claiming_a_price(self):
        from .models import TenderSourceItem
        TenderSourceItem.objects.create(tender=self.tender, source_key='orphan-proposal', source_type='document_proposal', original_text='Историческое предложение', is_active=False, requirements={})
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['document_proposal_groups'][0]['can_keep_quote'])


    def test_identical_document_confirmation_does_not_create_a_review(self):
        parent = self.parents[0]
        parent.requirements = {"characteristics": [{"name": "Материал", "value": "бумага"}]}
        parent.save()
        result = self.stage([self.row(parent, relationship="enrich", name=parent.original_text, quantity=parent.quantity, unit=parent.unit, supply_kind="finished", requirements={"reconciled": True, "keep_refs": [0], "characteristics": []})])
        self.assertEqual(result["state"], "no_change")
        self.assertFalse(result["created"])
        historical = TenderSourceItem.objects.create(tender=self.tender, parent=parent, source_type="document_proposal", source_key="historical-confirmation", original_text=parent.original_text, quantity=parent.quantity, unit=parent.unit, requirements=parent.requirements, metadata={"relationship": "enrich", "supply_kind": "finished"}, is_active=False)
        from tenders.analysis_presentation import resolve_tender_analysis_status
        from tenders.models import TenderComputeJob
        TenderComputeJob.objects.create(tender=self.tender, version="v2-test", status="needs_review")
        self.assertEqual(resolve_tender_analysis_status(self.tender).pending_count, 0)
        response = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))
        self.assertFalse(response.context["document_proposal_groups"])
        self.assertTrue(TenderSourceItem.objects.filter(pk=historical.pk).exists())

    def test_review_shows_only_new_facts_and_accepts_real_snapshot_with_old_confirmation(self):
        parent = self.parents[0]
        parent.requirements = {"characteristics": [{"name": "Материал", "value": "бумага"}]}
        parent.save()
        TenderSourceItem.objects.create(tender=self.tender, parent=parent, source_type="document_proposal", source_key="old-confirmation", original_text=parent.original_text, quantity=parent.quantity, unit=parent.unit, requirements=parent.requirements, metadata={"relationship": "enrich"}, is_active=False)
        proposal = self.stage([self.row(parent, relationship="enrich", name=parent.original_text, quantity=parent.quantity, unit=parent.unit, supply_kind="finished", requirements={"reconciled": True, "keep_refs": [0], "characteristics": [{"name": "Печать", "value": "двусторонняя", "resolution": "new", "baseline_refs": [], "source_url": "https://zakupki.gov.ru/components"}]})])["created"][0]
        response = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))
        rows = response.context["document_proposal_groups"][0]["items"][0]
        self.assertEqual([row["name"] for row in rows["changed_rows"]], ["Печать"])
        self.assertFalse(rows["removed_rows"])
        self.assertEqual(len(rows["tz_rows"]), 2)
        from tender_selection.services import decide_all_document_proposals
        result = decide_all_document_proposals(self.admin, self.tender.pk, "accept", [proposal.pk])
        self.assertEqual(result["changed"], 1)
