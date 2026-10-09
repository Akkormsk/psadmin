from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from tender_selection.models import Tender
from .models import CalculationComponent, ComponentOperationStep, ComponentRoutePlan, ProcessDefinition, TenderCommercialItem, TenderComputeJob, TenderComputeLine, TenderSourceItem
from .provider_execution import calculate_routed_provider_lines
from .provider_knowledge import confirm_knowledge, create_knowledge_draft, create_provider, initialize_structured_rules_binding


class ProviderExecutionTests(TestCase):
    def test_pipeline_line_persists_confirmed_sewing_quote(self):
        user = get_user_model().objects.create_user("pipeline-sewing")
        stage = ProcessDefinition.objects.create(name="Пошив V2 test", role="production")
        provider, staging = create_provider(user, "Швейное V2 test", [stage])
        data = {"formula_status": "confirmed", "requires_confirmation": False, "pricing": {"currency": "RUB", "variants": {"Футболка | Классическая женская | Кулирка": {"unit_price": "414.8", "minimum_quantity": "100", "source_row": 8}}}}
        binding = initialize_structured_rules_binding(confirm_knowledge(create_knowledge_draft(provider, user, data, staging, stage), user))
        tender = Tender.objects.create(purchase_number="sewing-pipeline", title="Пошив")
        source = TenderSourceItem.objects.create(tender=tender, source_key="shirt", source_type="manual", original_text="Футболка")
        job = TenderComputeJob.objects.create(tender=tender)
        commercial = TenderCommercialItem.objects.create(tender=tender, job=job, source_key="shirt", display_name="Футболка", quantity=Decimal("100"))
        component = CalculationComponent.objects.create(commercial_item=commercial, source_item=source, name="Футболка")
        plan = ComponentRoutePlan.objects.create(commercial_item=commercial, component=component)
        ComponentOperationStep.objects.create(route_plan=plan, process=stage, position=1)
        line = TenderComputeLine.objects.create(job=job, source_item=source, commercial_item=commercial, component=component, input_snapshot={"provider_calculator": {"variant": "Футболка | Классическая женская | Кулирка"}})
        self.assertEqual(calculate_routed_provider_lines(job), [line])
        line.refresh_from_db()
        self.assertEqual(line.status, "ready")
        self.assertEqual(line.result["provider_binding_id"], binding.pk)
        self.assertEqual(line.result["provider_quote"]["total"], "41480.0000000")
