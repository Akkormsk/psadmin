from django.contrib.auth import get_user_model
from django.test import TestCase

from tender_selection.models import Tender

from .catalog_preparation import CatalogPreparationEngine
from .models import (
    CalculationComponent,
    ComponentOperationStep,
    ComponentRoutePlan,
    Counterparty,
    ProcessDefinition,
    StageCounterpartyLink,
    TenderCommercialItem,
    TenderComputeJob,
    TenderComputePreparation,
)
from .provider_capabilities import CounterpartyCapabilityPreparationEngine, resolve_provider_candidates


class ProviderCapabilityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("provider-capability", password="test")
        self.tender = Tender.objects.create(purchase_number="provider-capability", title="Tender")
        self.job = TenderComputeJob.objects.create(tender=self.tender)
        self.commercial = TenderCommercialItem.objects.create(
            tender=self.tender, job=self.job, source_key="item", display_name="Item",
        )
        self.component = CalculationComponent.objects.create(commercial_item=self.commercial, name="Item")

    def step(self, process, position=1):
        plan = ComponentRoutePlan.objects.create(commercial_item=self.commercial, component=self.component)
        return ComponentOperationStep.objects.create(route_plan=plan, process=process, position=position)

    def counterparty(self, name):
        return Counterparty.objects.create(name=name, created_by=self.user)

    def test_existing_link_is_the_generic_candidate_source_and_many_to_many_is_preserved(self):
        universal = ProcessDefinition.objects.create(name="Universal external execution", role="production")
        branding = ProcessDefinition.objects.create(name="Branding external execution", role="production")
        provider_a = self.counterparty("Provider A")
        provider_b = self.counterparty("Provider B")
        StageCounterpartyLink.objects.create(stage=universal, counterparty=provider_a, priority=1)
        StageCounterpartyLink.objects.create(stage=universal, counterparty=provider_b, priority=2)
        StageCounterpartyLink.objects.create(stage=branding, counterparty=provider_a, priority=1)

        universal_candidates = resolve_provider_candidates(self.component, self.step(universal))
        branding_candidates = resolve_provider_candidates(self.component, self.step(branding, position=2))

        self.assertEqual([candidate.counterparty_id for candidate in universal_candidates], [provider_a.pk, provider_b.pk])
        self.assertEqual([candidate.counterparty_id for candidate in branding_candidates], [provider_a.pk])
        self.assertEqual(universal_candidates[0].counterparty_id, branding_candidates[0].counterparty_id)

    def test_inactive_counterparty_or_link_is_excluded(self):
        process = ProcessDefinition.objects.create(name="Capability", role="production")
        active = self.counterparty("Active")
        inactive = self.counterparty("Inactive")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        StageCounterpartyLink.objects.create(stage=process, counterparty=active)
        StageCounterpartyLink.objects.create(stage=process, counterparty=inactive)
        StageCounterpartyLink.objects.create(stage=process, counterparty=self.counterparty("Disabled"), is_active=False)

        self.assertEqual([candidate.counterparty_name for candidate in resolve_provider_candidates(self.component, self.step(process))], ["Active"])

    def test_provider_engine_persists_candidates_without_a_quote_request(self):
        process = ProcessDefinition.objects.create(name="Contractor capability", role="production")
        provider = self.counterparty("Provider")
        link = StageCounterpartyLink.objects.create(stage=process, counterparty=provider, price_source_type="manual_quote")
        step = self.step(process)
        engine = CounterpartyCapabilityPreparationEngine()

        task = engine.plan_tender_preparation(self.job, [step])[0]
        unit = self.job.work_units.create(engine_key=engine.key, dedupe_key=task.dedupe_key, input_fingerprint=task.fingerprint)
        unit.operation_steps.add(step)
        preparation = TenderComputePreparation.objects.create(
            work_unit=unit, engine_key=engine.key, preparation_key="prepare", payload=task.payload,
        )
        result = engine.prepare(preparation)

        preparation.refresh_from_db()
        candidate = preparation.payload["provider_candidates"][0]["candidates"][0]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(candidate["counterparty_id"], provider.pk)
        self.assertEqual(candidate["link_id"], link.pk)
        self.assertEqual(candidate["price_source_type"], "manual_quote")

    def test_contractor_capability_never_becomes_fake_catalog_purchase(self):
        process = ProcessDefinition.objects.create(name="Contractor capability", role="production", supplies_input=False)
        StageCounterpartyLink.objects.create(stage=process, counterparty=self.counterparty("Provider"))
        step = self.step(process)

        self.assertTrue(CounterpartyCapabilityPreparationEngine().supports_preparation(step))
        self.assertFalse(CatalogPreparationEngine().supports_preparation(step))

    def test_catalog_capability_remains_supported_by_catalog_engine(self):
        process = ProcessDefinition.objects.create(name="Ready-made supplier", role="supply", supplies_input=True)

        self.assertTrue(CatalogPreparationEngine().supports_preparation(self.step(process)))
