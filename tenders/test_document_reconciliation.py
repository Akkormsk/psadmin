from django.test import TestCase
from django.urls import reverse
from tender_selection.services import decide_document_proposals
from . import test_document_components as fixtures
from .models import TenderSourceItem


class DocumentReconciliationTests(TestCase):
    row = fixtures.DocumentComponentAcceptanceTests.row
    stage = fixtures.DocumentComponentAcceptanceTests.stage

    def setUp(self):
        fixtures.DocumentComponentAcceptanceTests.setUp(self)
        self.parent = self.parents[0]
        self.parent.requirements = {'characteristics': [{'name': 'Материал корпуса', 'value': 'металл'}, {'name': 'Нанесение', 'value': 'лазерная гравировка'}, {'name': 'Цвет', 'value': 'синий'}]}
        self.parent.save(update_fields=['requirements'])

    def proposal(self, **changes):
        args = {'relationship': 'enrich', 'quantity': 100, 'requirements': {'characteristics': [{'name': 'Упаковка', 'value': 'индивидуальная'}, {'name': 'Цвет', 'value': 'синий'}]}}
        args.update(changes)
        return self.stage([self.row(self.parent, **args)])['created'][0]

    def test_original_facts_remain_plain_before_and_after_acceptance_and_reanalysis(self):
        proposal = self.proposal()
        url = reverse('tender_selection:detail', args=[self.tender.pk])
        def verify(facts):
            original = next(row for row in facts if row['name'] == 'Материал корпуса')
            addition = next(row for row in facts if row['name'] == 'Упаковка')
            self.assertFalse(any(part['added'] for part in original['parts']))
            self.assertFalse(original['name_added'])
            self.assertTrue(all(part['added'] for part in addition['parts']))
        response = self.client.get(url)
        verify(response.context['document_proposal_groups'][0]['items'][0]['characteristics'])
        decide_document_proposals(self.admin, self.tender.pk, self.parent.pk, 'accept')
        response = self.client.get(url)
        verify(next(row for row in response.context['display_items'] if row['name'] == proposal.original_text)['characteristics'])
        proposal.refresh_from_db()
        self.stage([self.row(self.parent, parent_source_item_id=proposal.pk, relationship='enrich', quantity=100, requirements={'reconciled': True, 'characteristics': [{'resolution': 'keep', 'baseline_refs': [index]} for index in range(len(proposal.requirements['characteristics']))]})])['created'][0]
        response = self.client.get(url)
        verify(response.context['document_proposal_groups'][0]['items'][0]['characteristics'])

    def test_staging_preserves_original_facts_and_removes_exact_duplicates(self):
        proposal = self.proposal()
        facts = proposal.requirements['characteristics']
        self.assertIn({'name': 'Материал корпуса', 'value': 'металл'}, [{k: row[k] for k in ('name', 'value')} for row in facts])
        self.assertEqual(sum(row['name'] == 'Цвет' for row in facts), 1)
        self.assertTrue(any(row['name'] == 'Упаковка' for row in facts))

    def test_legacy_accepted_enrichment_recovers_facts_codes_and_money_for_display(self):
        proposal = self.proposal()
        proposal.requirements = {'characteristics': [{'name': 'Упаковка', 'value': 'индивидуальная'}]}
        proposal.source_type, proposal.is_active = 'document_extraction', True
        proposal.save()
        self.parent.is_active = False; self.parent.save(update_fields=['is_active'])
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]) + '?workspace=1')
        shown = next(row for row in response.context['display_items'] if row['name'] == proposal.original_text)
        self.assertIsNotNone(shown['price'])
        self.assertIsNotNone(shown['sum'])
        self.assertTrue(any(row['name'] == 'Материал корпуса' for row in shown['characteristics']))
        proposal.refresh_from_db()
        self.assertEqual(len(proposal.requirements['characteristics']), 1)  # Display repair is read-only.

    def test_acceptance_rechecks_the_merge_not_only_the_preview(self):
        proposal = self.proposal()
        proposal.requirements = {'characteristics': [{'name': 'Упаковка', 'value': 'индивидуальная'}]}
        proposal.save(update_fields=['requirements'])
        decide_document_proposals(self.admin, self.tender.pk, self.parent.pk, 'accept')
        proposal.refresh_from_db()
        self.assertTrue(any(row['name'] == 'Материал корпуса' for row in proposal.requirements['characteristics']))

    def test_components_keep_parent_cost_once_without_copying_it_to_children(self):
        proposals = self.stage([self.row(self.parent, name='Часть А'), self.row(self.parent, name='Часть Б')])['created']
        decide_document_proposals(self.admin, self.tender.pk, self.parent.pk, 'accept')
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        for shown in response.context['display_items']:
            if shown['name'] in {'Часть А', 'Часть Б'}:
                self.assertIsNone(shown['price'])
                self.assertFalse(any(row['name'] == 'Материал корпуса' for row in shown['characteristics']))
        financials = response.context['source_financials']
        self.assertEqual(len(financials), 1)
        self.assertIsNotNone(financials[0]['sum'])

    def test_conflicting_values_are_both_visible_before_acceptance(self):
        proposal = self.proposal(requirements={'characteristics': [{'name': 'Материал корпуса', 'value': 'пластик'}]})
        materials = [row for row in proposal.requirements['characteristics'] if row['name'] == 'Материал корпуса']
        self.assertEqual({row['value'] for row in materials}, {'металл', 'пластик'})
        self.assertTrue(all(row['conflict'] for row in materials))
        card = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        self.assertContains(card, 'Расхождение источников')

    def test_enrichment_of_component_does_not_inherit_the_whole_bundle_price(self):
        component = self.stage([self.row(self.parent)])['created'][0]
        decide_document_proposals(self.admin, self.tender.pk, self.parent.pk, 'accept')
        enrichment = TenderSourceItem.objects.create(tender=self.tender, parent=component, source_type='document_extraction', source_key='component-enrichment', original_text='Уточнённый компонент', quantity=component.quantity, unit=component.unit, requirements={'characteristics': [{'name': 'Упаковка', 'value': 'пакет'}]}, metadata={'relationship': 'enrich'})
        component.is_active = False; component.save(update_fields=['is_active'])
        card = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        shown = next(row for row in card.context['display_items'] if row['name'] == enrichment.original_text)
        self.assertIsNone(shown['price'])
        self.assertEqual(len(card.context['source_financials']), 1)
        self.assertTrue(any(row['name'] == 'Цвет' for row in shown['characteristics']))

    def test_simple_commercial_projection_preserves_original_facts_and_quote(self):
        from .models import TenderCommercialItem
        item = TenderCommercialItem.objects.create(tender=self.tender, source_key='simple', display_name='Изделие', quantity=self.parent.quantity, unit=self.parent.unit, requirements={'characteristics': [{'name': 'Упаковка', 'value': 'пакет'}]})
        item.source_items.add(self.parent)
        card = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        shown = card.context['display_items'][0]
        self.assertIsNotNone(shown['price'])
        self.assertTrue(any(row['name'] == 'Материал корпуса' for row in shown['characteristics']))

    def test_quantity_change_does_not_reassign_original_unit_price(self):
        proposal = self.proposal(quantity=200)
        decide_document_proposals(self.admin, self.tender.pk, self.parent.pk, 'accept')
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        shown = next(row for row in response.context['display_items'] if row['name'] == proposal.original_text)
        self.assertIsNone(shown['price'])
        self.assertEqual(len(response.context['source_financials']), 1)
        self.assertIsNotNone(response.context['source_financials'][0]['sum'])

    def test_independent_document_rows_cannot_duplicate_one_atomic_item(self):
        result = self.stage([self.row(self.parent, relationship='enrich'), self.row(self.parent, relationship='enrich', name='Другая формулировка')])
        self.assertEqual(result['created'], [])
        self.assertEqual(result['state'], 'validation_failure')

    def test_original_quote_is_preserved_exactly_without_recalculation(self):
        from decimal import Decimal
        original = self.tender.notification_raw['source']['notificationInfo']['purchaseObjectsInfo']['notDrugPurchaseObjectsInfo']['purchaseObject'][0]
        original.update(quantity={'value': '800'}, price='358', sum='286184')
        self.tender.save(update_fields=['notification_raw'])
        self.parent.quantity = 800; self.parent.save(update_fields=['quantity'])
        proposal = self.proposal(quantity=800)
        decide_document_proposals(self.admin, self.tender.pk, self.parent.pk, 'accept')
        card = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        shown = next(row for row in card.context['display_items'] if row['name'] == proposal.original_text)
        self.assertEqual(Decimal(str(shown['price'])), Decimal('358'))
        self.assertEqual(Decimal(str(shown['sum'])), Decimal('286184'))
        self.assertEqual(shown['quantity'], 800)

    def test_reanalysis_receives_the_whole_baseline_and_original_quote(self):
        from unittest.mock import patch
        from .calculation_v2_pipeline import GatewayDocumentEnricher, active_calculation_items, assess_quality
        proposal = self.proposal()
        decide_document_proposals(self.admin, self.tender.pk, self.parent.pk, 'accept')
        items = active_calculation_items(self.tender)
        with patch('tenders.services._ai_gateway_json', return_value=({'items': []}, {})) as gateway:
            GatewayDocumentEnricher().extract_tender(tender=self.tender, source_items=items, documents=assess_quality(self.tender, items)['documents'])
        prompt = gateway.call_args.args[0]
        self.assertIn('Материал корпуса', prompt)
        self.assertIn('индивидуальная', prompt)
        self.assertIn('notification_unit_price', prompt)
        self.assertIn('1000', prompt)

    def test_operator_report_reads_saved_data_without_any_writes_or_model_calls(self):
        from unittest.mock import patch
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from scripts.inspect_tender_analysis import collect_report
        proposal = self.proposal()
        decide_document_proposals(self.admin, self.tender.pk, self.parent.pk, 'accept')
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Paid call forbidden')), CaptureQueriesContext(connection) as queries:
            report = collect_report(self.tender.purchase_number)
        self.assertTrue(report['read_only'])
        self.assertFalse(report['model_called'])
        self.assertTrue(all(row['sql'].lstrip().upper().startswith('SELECT') for row in queries))
        item = next(row for row in report['active_items'] if row['name'] == proposal.original_text)
        self.assertEqual(item['relationship'], 'enrich')
        self.assertTrue(item['same_item_quote_eligible'])
        self.assertTrue(any(row['name'] == 'Материал корпуса' for row in item['effective_facts']))
