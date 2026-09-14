from django.test import SimpleTestCase

from tenders import gateway_budget


class IsChatRowTests(SimpleTestCase):
    def test_explicit_chat_mode_is_chat(self):
        self.assertTrue(gateway_budget._is_chat_row({"id": "openai/gpt-4.1-mini", "mode": "chat"}))

    def test_explicit_non_chat_mode_is_rejected_even_with_a_chatty_looking_id(self):
        self.assertFalse(gateway_budget._is_chat_row({"id": "openai/gpt-5.3-codex", "mode": "responses"}))
        self.assertFalse(gateway_budget._is_chat_row({"id": "openai/gpt-image-2", "mode": "image_generation"}))
        self.assertFalse(gateway_budget._is_chat_row({"id": "openai/gpt-4o-mini-tts", "mode": "audio_speech"}))

    def test_missing_mode_falls_back_to_id_markers(self):
        # Реальные примеры с шлюза 14.09.2026, где "mode" не приходит вовсе.
        self.assertTrue(gateway_budget._is_chat_row({"id": "yandex/yandexgpt-pro-5.1"}))
        self.assertTrue(gateway_budget._is_chat_row({"id": "timeweb/gpt-oss-120b"}))
        self.assertFalse(gateway_budget._is_chat_row({"id": "timeweb/bge-m3"}))
        self.assertFalse(gateway_budget._is_chat_row({"id": "dashscope/text-embedding-v4"}))
        self.assertFalse(gateway_budget._is_chat_row({"id": "black_forest_labs/flux-2-pro"}))


class ModelCatalogTests(SimpleTestCase):
    def test_under_test_uses_the_fallback_rows(self):
        catalog = gateway_budget.model_catalog()
        ids = {entry["id"] for entry in catalog}
        self.assertEqual(ids, set(gateway_budget._FALLBACK_MODELS))

    def test_priced_model_shows_a_real_rate_not_a_guess(self):
        catalog = gateway_budget.model_catalog()
        entry = next(e for e in catalog if e["id"] == "openai/gpt-4.1-mini")
        self.assertEqual(entry["in_rub"], 54)
        self.assertEqual(entry["out_rub"], 216)
        self.assertIn("54/216", entry["display"])
        self.assertEqual(entry["label"], "GPT-4.1 mini")

    def test_unpriced_model_shows_no_invented_price(self):
        catalog = gateway_budget.model_catalog()
        entry = next(e for e in catalog if e["id"] == "openai/gpt-4.1-nano")
        self.assertIsNone(entry["in_rub"])
        self.assertIsNone(entry["out_rub"])
        self.assertNotIn("₽", entry["display"])

    def test_priced_models_sort_before_unpriced_ones(self):
        catalog = gateway_budget.model_catalog()
        priced_flags = [entry["in_rub"] is not None for entry in catalog]
        # once it turns False it must stay False — no unpriced model before a priced one
        self.assertEqual(priced_flags, sorted(priced_flags, reverse=True))

    def test_available_models_is_just_the_ids(self):
        self.assertEqual(
            set(gateway_budget.available_models()),
            {entry["id"] for entry in gateway_budget.model_catalog()},
        )


class SpendRubStillWorksTests(SimpleTestCase):
    def test_known_model_rate_unchanged_by_the_catalog_rewrite(self):
        cost = gateway_budget.spend_rub({"prompt_tokens": 1_000_000, "completion_tokens": 0}, "openai/gpt-4.1-mini")
        self.assertEqual(cost, 54)
