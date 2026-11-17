from decimal import Decimal

from django.test import TestCase

from tender_selection.services import decide_document_proposals
from . import test_document_components as fixtures
from .calculation_v2_pipeline import build_commercial_items
from .document_enrichment import notification_quote_source
from .models import TenderCommercialItem, TenderComputeJob, TenderComputeLine, TenderEstimate, TenderLine


class DocumentAcceptanceSmokeTests(TestCase):
    setUp = fixtures.DocumentComponentAcceptanceTests.setUp
    row = fixtures.DocumentComponentAcceptanceTests.row
    stage = fixtures.DocumentComponentAcceptanceTests.stage

    def test_accept_refreshes_same_working_good_without_rewriting_old_estimate(self):
        parent = self.parents[0]
        good = build_commercial_items(tender=self.tender, sources=[parent])[0]
        component = good.components.get()
        estimate = TenderEstimate.objects.create(owner=self.admin, tender=self.tender, tender_number='saved', name='Прежний расчёт')
        line = TenderLine.objects.create(estimate=estimate, commercial_item=good, name='Прежнее описание', quantity=100,
            nmck_unit=1000, requirements={'characteristics': [{'name': 'Цвет', 'value': 'красный'}]})
        proposal = self.stage([self.row(parent, relationship='enrich', quantity=100, name='Уточнённый товар')])['created'][0]
        # Speculative routes must not alter the accepted good before review.
        draft = build_commercial_items(tender=self.tender, sources=[proposal])[0]
        good.refresh_from_db()
        self.assertEqual(good.display_name, parent.original_text)
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        good.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(good.display_name, proposal.original_text)
        self.assertEqual(good.components.get(status='active').pk, component.pk)
        self.assertEqual(list(good.source_items.values_list('pk', flat=True)), [proposal.pk])
        self.assertEqual(draft.status, 'superseded')
        proposal.refresh_from_db()
        self.assertEqual(build_commercial_items(tender=self.tender, sources=[proposal])[0].pk, good.pk)
        self.assertEqual(good.components.get(status='active').pk, component.pk)
        line.refresh_from_db()
        self.assertEqual(line.name, 'Прежнее описание')
        self.assertEqual(line.requirements['characteristics'][0]['value'], 'красный')

    def test_kit_parts_keep_totals_and_do_not_inherit_whole_kit_price(self):
        parent = self.parents[0]
        old = build_commercial_items(tender=self.tender, sources=[parent])[0]
        result = self.stage([self.row(parent, name='Ручка', per_parent='2'), self.row(parent, name='Блокнот', per_parent='3')])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        old.refresh_from_db()
        self.assertEqual(old.status, 'superseded')
        goods = list(TenderCommercialItem.objects.filter(tender=self.tender, status='active'))
        self.assertEqual({good.quantity for good in goods}, {Decimal('200'), Decimal('300')})
        for good in goods:
            self.assertEqual(good.components.get().effective_quantity, good.quantity)
        for source in result['created']:
            self.assertIsNone(notification_quote_source(source))
        self.assertEqual(self.tender.notification_raw, self.payload)

    def test_completeness_stays_with_one_finished_product(self):
        parent = self.parents[0]
        proposal = self.stage([self.row(parent, relationship='enrich', quantity=100, supply_kind='finished',
            requirements={'characteristics': [{'name': 'Комплектация', 'value': 'чехол и инструкция', 'scope': 'completeness'}]})])['created'][0]
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        good = TenderCommercialItem.objects.get(tender=self.tender, status='active')
        self.assertEqual(good.components.count(), 1)
        self.assertTrue(any(fact.get('scope') == 'completeness' for fact in good.requirements['characteristics']))
        self.assertEqual(good.quantity, proposal.quantity)

    def test_reject_retains_evidence_already_used_by_proposed_route(self):
        parent = self.parents[0]
        proposal = self.stage([self.row(parent)])["created"][0]
        draft = build_commercial_items(tender=self.tender, sources=[proposal])[0]
        job = TenderComputeJob.objects.create(tender=self.tender, status='ready')
        TenderComputeLine.objects.create(job=job, source_item=proposal, commercial_item=draft)
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'reject', schedule_routes=False)
        proposal.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(proposal.source_type, 'document_rejected')
        self.assertEqual(draft.status, 'rejected')
        parent.refresh_from_db()
        self.assertTrue(parent.is_active)

    def test_partial_document_addition_keeps_unmentioned_notice_facts(self):
        parent = self.parents[0]
        parent.requirements = {'characteristics': [{'name': 'Материал', 'value': 'металл'}, {'name': 'Цвет', 'value': 'синий'}]}
        parent.save(update_fields=['requirements'])
        result = self.stage([self.row(parent, relationship='enrich', quantity=100, requirements={'reconciled': True,
            'characteristics': [{'name': 'Упаковка', 'value': 'индивидуальная', 'resolution': 'new', 'baseline_refs': []}]})])
        self.assertEqual(result['state'], 'needs_review')
        proposal = result['created'][0]
        self.assertEqual({fact['name'] for fact in proposal.requirements['characteristics']}, {'Материал', 'Цвет', 'Упаковка'})
