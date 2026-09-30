from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Counterparty, ProcessDefinition, StageCounterpartyLink
from .production_pricing import ProductionPricingError, price_stage


class PriceStageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="pricing-admin", password="test")
        self.stage = ProcessDefinition.objects.create(name="Ризография (тест)", role=ProcessDefinition.ROLE_PRODUCTION, performs_production=True)
        self.counterparty = Counterparty.objects.create(name="FSPrint (тест)", created_by=self.user)

    def _link(self, **overrides):
        defaults = dict(
            stage=self.stage, counterparty=self.counterparty,
            price_source_type=StageCounterpartyLink.SOURCE_INTERNAL_CALCULATOR,
            settings={
                "pricing_module": "tenders.integrations.fsprint_rizograf",
                "answer_mapping": {"тип бумаги": "paper_key", "формат": "format"},
            },
        )
        defaults.update(overrides)
        return StageCounterpartyLink.objects.create(**defaults)

    def test_no_links_is_a_clear_error_not_a_guess(self):
        with self.assertRaises(ProductionPricingError) as ctx:
            price_stage(self.stage.pk, 1000, {})
        self.assertIn("не привязан", str(ctx.exception))

    def test_internal_calculator_computes_a_real_price_from_session_answers(self):
        self._link()
        hypothesis = {
            "questions": [{"id": f"req-{self.stage.pk}-тип бумаги", "text": "тип бумаги"}],
            "question_answers": {f"req-{self.stage.pk}-тип бумаги": "standard_ru_80"},
        }
        result = price_stage(self.stage.pk, 1000, hypothesis)
        self.assertEqual(result.counterparty_name, "FSPrint (тест)")
        self.assertEqual(result.unit_cost, result.total_cost / 1000)
        self.assertGreater(result.total_cost, 0)

    def test_missing_required_answer_is_reported_not_guessed(self):
        self._link()
        with self.assertRaises(ProductionPricingError) as ctx:
            price_stage(self.stage.pk, 1000, {})  # ни один ответ не дан — paper_key не передан
        self.assertIn("не хватает параметров", str(ctx.exception))

    def test_inactive_link_is_not_considered(self):
        self._link(is_active=False)
        with self.assertRaises(ProductionPricingError):
            price_stage(self.stage.pk, 1000, {})

    def test_unregistered_price_source_falls_through_to_the_next_priority_contractor(self):
        self._link(price_source_type=StageCounterpartyLink.SOURCE_MANUAL_QUOTE, priority=0)
        second = Counterparty.objects.create(name="Резервный поставщик", created_by=self.user)
        StageCounterpartyLink.objects.create(
            stage=self.stage, counterparty=second, priority=1,
            price_source_type=StageCounterpartyLink.SOURCE_INTERNAL_CALCULATOR,
            settings={"pricing_module": "tenders.integrations.fsprint_rizograf", "answer_mapping": {"тип бумаги": "paper_key"}},
        )
        hypothesis = {
            "questions": [{"id": f"req-{self.stage.pk}-тип бумаги", "text": "тип бумаги"}],
            "question_answers": {f"req-{self.stage.pk}-тип бумаги": "standard_ru_80"},
        }
        result = price_stage(self.stage.pk, 1000, hypothesis)
        self.assertEqual(result.counterparty_name, "Резервный поставщик")

    def test_unknown_module_is_a_clear_error(self):
        self._link(settings={"pricing_module": "tenders.integrations.does_not_exist"})
        with self.assertRaises(ProductionPricingError) as ctx:
            price_stage(self.stage.pk, 1000, {})
        self.assertIn("не найден", str(ctx.exception))
