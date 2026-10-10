from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase
from openpyxl import Workbook

from .sewing_price_list import canonical_data_from_rows, parse_sewing_workbook_bytes
from .models import ProcessDefinition
from .provider_calculators import calculate_provider
from .provider_knowledge import confirm_knowledge, confirm_sewing_price_list, create_knowledge_draft, create_provider, initialize_structured_rules_binding


class SewingPriceListTests(TestCase):
    def test_xlsx_uses_cached_cell_values(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Изделие", "Крой", "Ткань", "Тираж (мин)", "Комментарии", "Стоимость"])
        sheet.append(["Футболка", "Классическая женская", "Кулирка", 100, "Классическая женская футболка", 414.8])
        raw = BytesIO()
        workbook.save(raw)
        data = parse_sewing_workbook_bytes(raw.getvalue())
        self.assertIn("Футболка | Классическая женская | Кулирка", data["pricing"]["variants"])

    def test_keeps_real_values_but_marks_currency_and_formula_unconfirmed(self):
        data = canonical_data_from_rows([["", "", "", "", "Курс $", 80], ["", "", "", "", "+%", 82.4], ["Изделие", "Крой", "Ткань", "Тираж (мин)", "Комментарии", "Стоимость"], ["Футболка", "Классическая женская", "Кулирка", 100, "Классическая женская футболка", 414.8]])
        self.assertTrue(data["requires_confirmation"])
        self.assertIsNone(data["pricing"]["currency"])
        self.assertEqual(data["pricing"]["exchange_rate"], "80")
        self.assertEqual(data["pricing"]["variants"]["Футболка | Классическая женская | Кулирка"]["unit_price"], "414.8")

    def test_confirmed_xls_creates_one_binding_and_calculates(self):
        user = get_user_model().objects.create_user(username="sewing-price-user")
        stage, _ = ProcessDefinition.objects.get_or_create(name="Пошив", role="production")
        provider, staging = create_provider(user, "Швейное производство под ключ", [stage], extracted_text="Пошив.xls")
        data = canonical_data_from_rows([["", "", "", "", "Курс $", 80], ["", "", "", "", "+%", 82.4], ["Изделие", "Крой", "Ткань", "Тираж (мин)", "Комментарии", "Стоимость"], ["Футболка", "Классическая женская", "Кулирка", 100, "Классическая женская футболка", 414.8]])
        draft = create_knowledge_draft(provider, user, data, staging, stage)
        with self.assertRaises(ValueError):
            confirm_knowledge(draft, user)
        data.update(requires_confirmation=False, formula_status="confirmed")
        data["pricing"]["currency"] = "RUB"
        version = confirm_knowledge(create_knowledge_draft(provider, user, data, stage=stage), user)
        binding = initialize_structured_rules_binding(version)
        self.assertEqual(binding.pk, initialize_structured_rules_binding(version).pk)
        result = calculate_provider(binding, {"quantity": "100", "variant": "Футболка | Классическая женская | Кулирка"})
        self.assertEqual(result["total"], "41480.0")

    def test_dialogue_confirmation_reuses_existing_provider_and_binding(self):
        user = get_user_model().objects.create_user(username="dialogue-sewing-user")
        stage, _ = ProcessDefinition.objects.get_or_create(name="Пошив", role="production")
        provider, _ = create_provider(user, "Швейное производство под ключ", [stage], extracted_text="старый прайс")
        data = canonical_data_from_rows([["", "", "", "", "Курс $", 80], ["", "", "", "", "+%", 82.4], ["Изделие", "Крой", "Ткань", "Тираж (мин)", "Комментарии", "Стоимость"], ["Футболка", "Классическая женская", "Кулирка", 100, "Классическая женская футболка", 414.8]])
        draft = create_knowledge_draft(provider, user, data, stage=stage)
        confirmed, binding = confirm_sewing_price_list(draft, user, "RUB", True)
        self.assertEqual(confirmed.status, "confirmed")
        self.assertEqual(binding.pk, initialize_structured_rules_binding(confirmed).pk)
