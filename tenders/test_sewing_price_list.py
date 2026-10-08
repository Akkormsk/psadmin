from django.contrib.auth import get_user_model
from django.test import TestCase

from .sewing_price_list import canonical_data_from_rows
from .models import ProcessDefinition, ProviderCalculatorBinding
from .provider_calculators import ProviderCalculatorError, calculate_provider
from .provider_knowledge import confirm_knowledge, create_knowledge_draft, create_provider


class SewingPriceListTests(TestCase):
    def test_keeps_real_values_but_marks_currency_and_formula_unconfirmed(self):
        rows = [
            ["", "", "", "", "Курс $", 80],
            ["", "", "", "", "+%", 82.4],
            ["Изделие", "Крой", "Ткань", "Тираж (мин)", "Комментарии", "Стоимость"],
            ["Футболка", "Классическая мужская", "Кулирка", "", "Классическая мужская футболка", 414.8],
            ["", "Классическая женская", "Кулирка", 100, "Классическая женская футболка", 414.8],
        ]

        data = canonical_data_from_rows(rows)

        self.assertTrue(data["requires_confirmation"])
        self.assertEqual(data["formula_status"], "unresolved")
        self.assertIsNone(data["pricing"]["currency"])
        self.assertEqual(data["pricing"]["exchange_rate"], "80")
        variant = data["pricing"]["variants"]["Футболка | Классическая женская | Кулирка"]
        self.assertEqual(variant["minimum_quantity"], "100")
        self.assertEqual(variant["unit_price"], "414.8")

    def test_calculates_confirmed_variant_and_rejects_unconfirmed_xls_rules(self):
        user = get_user_model().objects.create_user(username="sewing-price-user")
        stage, _ = ProcessDefinition.objects.get_or_create(name="Пошив", role="production")
        provider, staging = create_provider(user, "Швейное производство под ключ", [stage], extracted_text="Пошив.xls")
        data = canonical_data_from_rows([
            ["", "", "", "", "Курс $", 80],
            ["", "", "", "", "+%", 82.4],
            ["Изделие", "Крой", "Ткань", "Тираж (мин)", "Комментарии", "Стоимость"],
            ["Футболка", "Классическая женская", "Кулирка", 100, "Классическая женская футболка", 414.8],
        ])
        draft = create_knowledge_draft(provider, user, data, staging, stage)
        version = confirm_knowledge(draft, user)
        binding = ProviderCalculatorBinding.objects.create(
            link=provider.stage_links.get(stage=stage), knowledge_version=version,
            calculator_type=ProviderCalculatorBinding.TYPE_STRUCTURED_RULES,
        )

        with self.assertRaises(ProviderCalculatorError):
            calculate_provider(binding, {"quantity": "100", "variant": "Футболка | Классическая женская | Кулирка"})

        data["requires_confirmation"] = False
        data["formula_status"] = "confirmed"
        data["pricing"]["currency"] = "RUB"
        confirmed = confirm_knowledge(create_knowledge_draft(provider, user, data, stage=stage), user)
        binding.knowledge_version = confirmed
        binding.save(update_fields=["knowledge_version"])

        result = calculate_provider(binding, {"quantity": "100", "variant": "Футболка | Классическая женская | Кулирка"})

        self.assertEqual(result["currency"], "RUB")
        self.assertEqual(result["total"], "41480.0")
        self.assertEqual(result["unit_price"], "414.8")
