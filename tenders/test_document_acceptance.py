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


class DocumentRepeatSmokeTests(DocumentAcceptanceSmokeTests):
    """Repeat the accepted kit through the real shared writer; never call paid APIs."""
    def test_badge_wording_does_not_create_another_notice_position(self):
        from unittest.mock import patch
        from tender_selection.national_regime import LABELS
        from .calculation_v2_pipeline import ingest_source_items
        before = [row.pk for row in self.parents]
        with patch.dict(LABELS, {'unknown': ('gray', 'Сведения не указаны', 'Изменён только текст бейджа')}):
            self.assertEqual([row.pk for row in ingest_source_items(self.tender)], before)
        self.assertEqual(self.tender.v2_source_items.count(), len(before))

    def test_identical_accepted_composition_does_not_request_another_decision(self):
        parent = self.parents[0]
        row = self.row(parent)
        self.stage([row])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        accepted = parent.derived_items.get(is_active=True)
        row['accepted_source_item_id'] = accepted.pk
        result = self.stage([row])
        self.assertEqual(result['state'], 'no_change')
        self.assertFalse(result['created'])
        accepted.refresh_from_db()
        self.assertTrue(accepted.is_active)
        self.assertEqual(accepted.source_type, 'document_extraction')

    def test_one_changed_part_keeps_unchanged_siblings_and_does_not_highlight_same_quantity(self):
        from .document_enrichment import document_proposal_changes
        parent = self.parents[0]
        rows = [self.row(parent, name='Изделие А'), self.row(parent, name='Изделие Б')]
        self.stage(rows)
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        for row, accepted in zip(rows, parent.derived_items.filter(is_active=True).order_by('pk')):
            row['accepted_source_item_id'] = accepted.pk
        rows[0]['requirements'] = {'characteristics': [{'name': 'Цвет', 'value': 'Красный'}]}
        result = self.stage(rows)
        self.assertEqual(len(result['created']), len(rows))
        for proposal in result['created']:
            changes = document_proposal_changes(proposal)
            self.assertFalse(changes['quantity'])
            self.assertFalse(changes['unit'])
        self.assertEqual([len(document_proposal_changes(row)['rows']) for row in result['created']], [1, 0])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        self.assertEqual(parent.derived_items.filter(is_active=True).count(), len(rows))

    def _duplicate_accepted_group(self, *, changed=False):
        from copy import deepcopy
        from .models import TenderSourceItem
        parent = self.parents[0]
        self.stage([self.row(parent)])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        source = parent.derived_items.get(is_active=True)
        duplicate_root = TenderSourceItem.objects.create(tender=self.tender, source_key=parent.source_key + ':v:old-label',
            source_type='notification', original_text=parent.original_text, quantity=parent.quantity, unit=parent.unit,
            requirements=deepcopy(parent.requirements), metadata=deepcopy(parent.metadata), is_active=False)
        duplicate = TenderSourceItem.objects.create(tender=self.tender, source_key='duplicate-evidence', parent=duplicate_root,
            source_type='document_extraction', original_text=source.original_text, quantity=source.quantity, unit=source.unit,
            requirements={'characteristics': [{'name': 'Цвет', 'value': 'Другой'}]} if changed else deepcopy(source.requirements),
            metadata=deepcopy(source.metadata), is_active=True)
        good = build_commercial_items(tender=self.tender, sources=[duplicate])[0]
        return source, duplicate, good

    def test_equal_duplicates_retire_without_deleting_goods_or_saved_estimate(self):
        from .tender_data import reconcile_duplicate_notice_items
        source, duplicate, good = self._duplicate_accepted_group()
        previous_good = source.commercial_items.get()
        estimate = TenderEstimate.objects.create(owner=self.admin, tender=self.tender, name='Сохранённый расчёт')
        line = TenderLine.objects.create(estimate=estimate, commercial_item=previous_good, name='Прежний товар', quantity=200, nmck_unit=100)
        preview = reconcile_duplicate_notice_items(self.tender)
        source.refresh_from_db()
        self.assertTrue(source.is_active)
        self.assertEqual(preview['source_aliases'], {source.pk: duplicate.pk})
        self.assertEqual(reconcile_duplicate_notice_items(self.tender, apply=True), preview)
        source.refresh_from_db(); previous_good.refresh_from_db(); good.refresh_from_db(); line.refresh_from_db()
        self.assertFalse(source.is_active)
        self.assertEqual(previous_good.status, 'superseded')
        self.assertEqual(good.status, 'active')
        self.assertEqual(line.commercial_item_id, previous_good.pk)
        self.assertEqual(line.nmck_unit, 100)
        self.assertFalse(reconcile_duplicate_notice_items(self.tender, apply=True)['source_aliases'])

    def test_different_accepted_requirements_are_not_merged_by_name(self):
        from .tender_data import reconcile_duplicate_notice_items
        source, duplicate, good = self._duplicate_accepted_group(changed=True)
        plan = reconcile_duplicate_notice_items(self.tender, apply=True)
        self.assertTrue(plan['conflicting_groups'])
        self.assertFalse(plan['source_aliases'])
        source.refresh_from_db(); duplicate.refresh_from_db()
        self.assertTrue(source.is_active)
        self.assertTrue(duplicate.is_active)


    def test_explicit_identity_survives_renaming_and_preserves_draft_boundary(self):
        from .document_enrichment import document_proposal_changes
        parent = self.parents[0]
        self.stage([self.row(parent, name='Изготовление изделия по техническому заданию')])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        accepted = parent.derived_items.get(is_active=True)
        current = accepted.commercial_items.get(status='active')
        row = self.row(parent, name='Изделие', accepted_source_item_id=accepted.pk)
        proposal = self.stage([row])['created'][0]
        changes = document_proposal_changes(proposal)
        self.assertTrue(changes['name'])
        self.assertFalse(changes['quantity'])
        self.assertFalse(changes['unit'])
        self.assertFalse(changes['removed'])
        build_commercial_items(tender=self.tender, sources=[proposal])
        current.refresh_from_db()
        self.assertEqual(current.status, 'active')
        self.assertEqual(list(current.source_items.values_list('pk', flat=True)), [accepted.pk])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        current.refresh_from_db()
        self.assertEqual(current.display_name, 'Изделие')
        self.assertEqual(current.status, 'active')
        self.assertEqual(list(current.source_items.values_list('pk', flat=True)), [proposal.pk])

    def test_real_quantity_change_compares_to_accepted_part_not_aggregate(self):
        from .document_enrichment import document_proposal_changes
        parent = self.parents[0]
        self.stage([self.row(parent)])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        accepted = parent.derived_items.get(is_active=True)
        proposal = self.stage([self.row(parent, per_parent='0.5', name='Уточнённое изделие',
                                       accepted_source_item_id=accepted.pk)])['created'][0]
        changes = document_proposal_changes(proposal)
        self.assertTrue(changes['quantity'])
        self.assertFalse(changes['unit'])
        self.assertEqual(proposal.quantity, parent.quantity * Decimal('0.5'))

    def test_another_groups_identity_is_rejected_without_staging(self):
        parent = self.parents[0]
        before = self.tender.v2_source_items.count()
        result = self.stage([self.row(parent, accepted_source_item_id=self.parents[1].pk)])
        self.assertEqual(result['state'], 'validation_failure')
        self.assertFalse(result['created'])
        self.assertEqual(self.tender.v2_source_items.count(), before)

    def test_staged_accepted_specification_survives_an_empty_model_addition(self):
        from unittest.mock import patch
        from .calculation_v2_pipeline import GatewayDocumentEnricher, assess_quality
        parent = self.parents[0]
        self.stage([self.row(parent)])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        accepted = parent.derived_items.get(is_active=True)
        engine = GatewayDocumentEnricher()
        with patch('tender_selection.services.documents_html'):
            engine.prepare_stages(self.tender, [parent], assess_quality(self.tender, [parent])['documents'])
        good = {'key': 'accepted-part', 'source_id': parent.pk, 'accepted_source_item_id': accepted.pk,
                'kind': 'kit', 'name': 'Изделие', 'provenance': {}}
        result = engine.reconcile_specifications([{'key': good['key'], 'characteristics': []}], [good])
        self.assertFalse(engine.specification_gaps)
        self.assertEqual([(fact['name'], fact['value']) for fact in result[good['key']]['characteristics']],
                         [(fact['name'], fact['value']) for fact in accepted.requirements['characteristics']])
        self.assertIn('accepted_items', engine.working_context[parent.pk])


    def test_completed_no_change_retires_old_proposal_without_touching_accepted_good(self):
        from tender_selection.services import _save_intake_proposals
        from .models import TenderSourceItem
        parent = self.parents[0]
        self.stage([self.row(parent)])
        decide_document_proposals(self.admin, self.tender.pk, parent.pk, 'accept', schedule_routes=False)
        accepted = parent.derived_items.get(is_active=True)
        old = TenderSourceItem.objects.create(tender=self.tender, parent=parent, source_key='legacy-review',
            source_type='document_proposal', original_text='Устаревшая формулировка', is_active=False)
        job = TenderComputeJob.objects.create(tender=self.tender, version='v2-intake-refresh')
        _save_intake_proposals(job, {'created': [], 'state': 'no_change'}, incomplete=False)
        accepted.refresh_from_db(); old.refresh_from_db()
        self.assertTrue(accepted.is_active)
        self.assertEqual(old.source_type, 'document_superseded')


class AcceptedRoutingFlowTests(TestCase):
    setUp = fixtures.DocumentComponentAcceptanceTests.setUp
    row = fixtures.DocumentComponentAcceptanceTests.row
    stage = fixtures.DocumentComponentAcceptanceTests.stage

    def router(self):
        from unittest.mock import Mock
        from .models import ProcessDefinition
        from .calculation_v2_pipeline import RouteDecision
        process = ProcessDefinition.objects.create(name='Исполнение')
        router = Mock(cost_rub=0, diagnostics={})
        router.route.side_effect = lambda **kw: [RouteDecision(item.pk, (process.pk,), Decimal('.9'), {}) for item in kw['items']]
        return router

    def run_routes(self, router):
        from tender_selection.services import _run_document_analysis
        job = TenderComputeJob.objects.create(tender=self.tender, version='v2-intake-refresh',
            status='queued', diagnostics={'routes_only': True})
        _run_document_analysis(job.pk, router=router)
        job.refresh_from_db()
        return job

    def test_pending_tz_waits_then_acceptance_routes_once_and_update_reuses(self):
        from unittest.mock import patch
        from django.urls import reverse
        from .models import ComponentRoutePlan
        proposal = self.stage([self.row(self.parents[0])])['created'][0]
        router = self.router()
        waiting = self.run_routes(router)
        router.route.assert_not_called()
        self.assertEqual(waiting.status, 'needs_review')
        self.assertEqual(waiting.diagnostics['stages']['routing']['outcome'], 'awaiting_review')
        self.assertFalse(TenderCommercialItem.objects.filter(source_items=proposal).exists())
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]), {'analysis_snapshot': '1', 'full_card': '1'})
        self.assertNotContains(response, f'data-route-source="{proposal.pk}"')
        with patch('tender_selection.services.refresh_document_analysis') as scheduled, self.captureOnCommitCallbacks(execute=True):
            decide_document_proposals(self.admin, self.tender.pk, self.parents[0].pk, 'accept')
        scheduled.assert_called_once_with(self.admin, self.tender.pk, routes_only=True)
        first = self.run_routes(router)
        self.assertEqual(first.status, 'ready')
        plans = list(ComponentRoutePlan.objects.values_list('pk', flat=True))
        second = self.run_routes(router)
        self.assertEqual(second.status, 'ready')
        self.assertEqual(router.route.call_count, 1)
        self.assertEqual(list(ComponentRoutePlan.objects.values_list('pk', flat=True)), plans)
        self.assertFalse(self.tender.estimates.exists())

    def test_legacy_route_for_exact_proposal_survives_promotion(self):
        from .models import ComponentRoutePlan, ComponentOperationStep
        from .calculation_v2_pipeline import source_route_fingerprint, _knowledge_for, source_route_snapshots
        proposal = self.stage([self.row(self.parents[0])])['created'][0]
        draft = build_commercial_items(tender=self.tender, sources=[proposal])[0]
        router = self.router()
        from .models import ProcessDefinition
        processes = list(ProcessDefinition.objects.filter(is_active=True).order_by('pk'))
        plan = ComponentRoutePlan.objects.create(commercial_item=draft, component=draft.components.get(),
            status='ready', metadata={'input_fingerprint': source_route_fingerprint(proposal, processes, _knowledge_for([])), 'confidence': '.9'})
        step = ComponentOperationStep.objects.create(route_plan=plan, process=processes[0], position=1)
        decide_document_proposals(self.admin, self.tender.pk, self.parents[0].pk, 'accept', schedule_routes=False)
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'ready')
        self.assertEqual(source_route_snapshots(self.tender)[proposal.pk]['state'], 'ready')
        self.run_routes(router)
        routed_ids = [item.source_item_id for item in router.route.call_args.kwargs['items']]
        self.assertNotIn(proposal.pk, routed_ids)
        self.assertEqual(plan.steps.get().pk, step.pk)
