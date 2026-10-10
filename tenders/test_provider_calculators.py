from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import CounterpartyKnowledgeVersion, ProcessDefinition, ProviderCalculatorBinding, ProviderCalculationQuote, StageCounterpartyLink
from .provider_calculators import calculate_provider, get_provider_calculator_schema
from .provider_knowledge import confirm_knowledge, create_knowledge_draft, create_provider


class ProviderKnowledgeCalculatorTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="provider-user")
        self.stage_a = ProcessDefinition.objects.create(name="Готовое изделие", role="supply")
        self.stage_b = ProcessDefinition.objects.create(name="Нанесение", role="production")
        self.provider, self.staging = create_provider(self.user, "Проверочный подрядчик", [self.stage_a, self.stage_b], extracted_text="прайс")

    def _confirmed(self, data):
        version = create_knowledge_draft(self.provider, self.user, data, self.staging, self.stage_a)
        return confirm_knowledge(version, self.user)

    def test_provider_links_multiple_capabilities_and_raw_is_not_needed_after_confirmation(self):
        self.assertEqual(self.provider.stage_links.count(), 2)
        version = self._confirmed({"pricing": {"tiers": [{"min": 1, "max": 10, "unit_price": "10"}]}})
        self.staging.refresh_from_db()
        self.assertEqual(self.staging.extracted_text, "")
        self.assertIsNone(self.staging.raw_content)
        binding = ProviderCalculatorBinding.objects.create(link=self.provider.stage_links.get(stage=self.stage_a), knowledge_version=version, calculator_type=ProviderCalculatorBinding.TYPE_STRUCTURED_RULES)
        self.assertEqual(calculate_provider(binding, {"quantity": "3"})["total"], "30")

    def test_confirmation_supersedes_previous_version(self):
        old = self._confirmed({"pricing": {"tiers": [{"min": 1, "unit_price": "10"}]}})
        fresh = create_knowledge_draft(self.provider, self.user, {"pricing": {"tiers": [{"min": 1, "unit_price": "11"}]}}, stage=self.stage_a)
        confirm_knowledge(fresh, self.user)
        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(old.status, CounterpartyKnowledgeVersion.STATUS_SUPERSEDED)
        self.assertEqual(fresh.status, CounterpartyKnowledgeVersion.STATUS_CONFIRMED)

    def test_structured_rules_tier_coefficient_minimum_and_trace(self):
        version = self._confirmed({"input_schema": [{"key": "urgent", "label": "Срочно", "required": False}], "pricing": {"tiers": [{"min": 1, "max": 50, "unit_price": "2"}, {"min": 51, "unit_price": "1"}], "fixed_fee": "5", "minimum_charge": "20", "coefficients": [{"input_key": "urgent", "multiplier": "1.5"}]}})
        binding = ProviderCalculatorBinding.objects.create(link=self.provider.stage_links.get(stage=self.stage_a), knowledge_version=version, calculator_type=ProviderCalculatorBinding.TYPE_STRUCTURED_RULES)
        result = calculate_provider(binding, {"quantity": "2", "urgent": "1"})
        self.assertEqual(result["total"], "20")
        self.assertEqual(ProviderCalculationQuote.objects.get().knowledge_version, version)
        self.assertEqual(get_provider_calculator_schema(binding)["inputs"][-1]["key"], "quantity")

    def test_manual_quote_uses_same_service_without_http(self):
        binding = ProviderCalculatorBinding.objects.create(link=self.provider.stage_links.get(stage=self.stage_a), calculator_type=ProviderCalculatorBinding.TYPE_MANUAL_QUOTE)
        self.assertEqual(calculate_provider(binding, {"quantity": "1"})["status"], ProviderCalculationQuote.STATUS_REQUIRES_QUOTE)
