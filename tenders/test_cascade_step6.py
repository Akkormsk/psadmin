"""Контракт шага 6: ограничение новых проверок, полнота ответа и кэш."""

from decimal import Decimal
from unittest.mock import patch

from .cascade import Cascade, Criterion
from .models import CascadeCache
from .test_cascade import TestCase, _Gateway, _line, _product


@patch.dict("os.environ", {"CASCADE_STEP6_FIRST": "25", "CASCADE_STEP6_CEILING": "75"})
class CascadeBoundedTests(TestCase):
    def setUp(self):
        super().setUp()
        self.cascade = Cascade(_line())
        self.cascade._tz_hash = "bounded-test"
        self.cascade.tz = [
            Criterion(label=label, raw_value="да", concept=label, operator="~", value="да")
            for label in ("Свойство А", "Свойство Б")
        ]

    def cards(self, count):
        return [
            {"id": str(i), "name": f"Товар {i}", "price": str(i + 1), "relevance": 0}
            for i in range(count)
        ]

    def grade(self, cards, grid):
        gateway = _Gateway(grid=grid)
        with patch("tenders.services._ai_gateway_json", side_effect=gateway):
            self.cascade.step_6_agent_matrix(cards)
        return gateway

    def test_stops_after_first_batch_and_preserves_ungraded_cards(self):
        cards = self.cards(71)
        gateway = self.grade(cards, [{"id": "*", "cells": {"1": "y", "2": "y"}}])
        self.assertEqual(gateway.calls["step6"], 4)
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 25)
        self.assertEqual(self.cascade.diagnostics["step6"]["batches"], 1)
        self.assertEqual(CascadeCache.objects.filter(kind="verdict").count(), 25)
        self.assertEqual(len(cards), 71)
        for card in cards[25:]:
            self.assertEqual(card["matrix_status"], "pending")
            self.assertEqual(card["unknown_count"], 2)
            self.assertNotEqual(card["fit"], "exact")

    def test_expands_when_first_batch_has_two_mismatches(self):
        grid = [{"id": str(i), "cells": {"1": "n", "2": "n"}} for i in range(25)]
        grid.append({"id": "*", "cells": {"1": "y", "2": "y"}})
        self.grade(self.cards(71), grid)
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 50)
        self.assertEqual(self.cascade.diagnostics["step6"]["batches"], 2)

    def test_ceiling_truncates_the_last_batch(self):
        with patch.dict("os.environ", {"CASCADE_STEP6_CEILING": "32"}):
            self.grade(self.cards(71), [{"id": "*", "cells": {"1": "n", "2": "n"}}])
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 32)
        self.assertEqual(CascadeCache.objects.filter(kind="verdict").count(), 32)

    def test_zero_ceiling_makes_no_calls(self):
        with patch.dict("os.environ", {"CASCADE_STEP6_CEILING": "0"}):
            gateway = self.grade(self.cards(71), [])
        self.assertEqual(gateway.calls["step6"], 0)

    def test_complete_unknowns_are_cached_but_do_not_stop_expansion(self):
        self.grade(self.cards(40), [{"id": "*", "cells": {"1": "m", "2": "m"}}])
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 40)
        self.assertEqual(self.cascade.diagnostics["step6"]["suitable"], 0)
        self.assertEqual(CascadeCache.objects.filter(kind="verdict").count(), 40)

    def test_missing_cells_are_not_cached_or_counted_as_complete(self):
        cards = self.cards(40)
        self.grade(cards, [{"id": "*", "cells": {"1": "y"}}])
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 40)
        self.assertEqual(self.cascade.diagnostics["step6"]["suitable"], 0)
        self.assertFalse(CascadeCache.objects.filter(kind="verdict").exists())
        self.assertTrue(all(c["matrix_status"] == "incomplete" for c in cards))

    def test_unambiguous_capacity_needs_no_model_answer(self):
        self.cascade.tz = [Criterion(
            label="Ёмкость", raw_value="32 ГБ", concept="ёмкость", operator=">=",
            value="32 ГБ", axis="capacity", num_min=Decimal(32768),
        )]
        cards = [{"id": "A", "name": "Флешка 32 ГБ", "price": "100", "relevance": 0}]
        gateway = self.grade(cards, [])
        self.assertEqual(gateway.calls["step6"], 0)
        self.assertEqual(cards[0]["matrix_status"], "complete")
        self.assertEqual(cards[0]["fit"], "exact")
        self.assertFalse(CascadeCache.objects.filter(kind="verdict").exists())

    def test_repeat_uses_cached_suitable_cards_without_grading_the_tail(self):
        grid = [{"id": "*", "cells": {"1": "y", "2": "y"}}]
        self.grade(self.cards(71), grid)
        gateway = self.grade(self.cards(71), grid)
        self.assertEqual(gateway.calls["step6"], 0)
        self.assertEqual(self.cascade.diagnostics["step6"]["cached"], 25)
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 0)

    def test_cached_cards_contribute_to_stop_but_not_to_new_budget(self):
        from .cascade import _cache_put

        for i in range(9):
            _cache_put("verdict", self.cascade._verdict_key(self.cards(9)[i]), {"grid": {"1": ["y", ""], "2": ["y", ""]}})
        grid = [{"id": "9", "cells": {"1": "y", "2": "y"}},
                {"id": "*", "cells": {"1": "n", "2": "n"}}]
        self.grade(self.cards(71), grid)
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 25)
        self.assertEqual(self.cascade.diagnostics["step6"]["suitable"], 10)

    def test_incomplete_legacy_cache_is_retried(self):
        from .cascade import _cache_put

        _cache_put("verdict", "bounded-test|0", {"grid": {"1": ["y", ""]}})
        gateway = self.grade(self.cards(1), [{"id": "*", "cells": {"1": "y", "2": "y"}}])
        self.assertEqual(gateway.calls["step6"], 1)

    def test_failed_gateway_stops_without_spending_on_more_batches(self):
        gateway = _Gateway(step6_error=True)
        with patch("tenders.services._ai_gateway_json", side_effect=gateway):
            self.cascade.step_6_agent_matrix(self.cards(71))
        self.assertEqual(gateway.calls["step6"], 4)
        self.assertFalse(CascadeCache.objects.filter(kind="verdict").exists())
        self.assertTrue(self.cascade.error)

    def test_preagent_key_uses_only_relevance_axis_verdicts_and_price(self):
        cards = [
            {"id": "bad", "relevance": 0, "price": "1"},
            {"id": "good", "relevance": 0, "price": "200"},
            {"id": "silent", "relevance": 0, "price": "2"},
            {"id": "less-relevant", "relevance": 1, "price": "0"},
        ]
        axes = {"bad": {1: ("n", "")}, "good": {1: ("y", "")},
                "less-relevant": {1: ("y", "")}}
        ranked = sorted(cards, key=lambda card: self.cascade._preagent_key(card, axes))
        self.assertEqual([c["id"] for c in ranked], ["good", "silent", "bad", "less-relevant"])

    def test_fully_deterministic_card_needs_no_model(self):
        self.cascade.tz = [Criterion("Длина", "6 см", "длина", "=", "6 см", "см")]
        cards = self.cards(1)
        cards[0]["attributes"] = [{"name": "Длина (мм)", "value": "58"}]
        gateway = self.grade(cards, [])
        self.assertEqual(gateway.calls["step6"], 0)
        self.assertEqual(cards[0]["matrix_status"], "complete")
        self.assertEqual(cards[0]["match_count"], 1)
        self.assertEqual(self.cascade.diagnostics["step6"]["deterministic"], 1)

    def test_partial_request_only_needs_unresolved_cells(self):
        self.cascade.tz[0] = Criterion("Длина", "6 см", "длина", "=", "6 см", "см")
        cards = self.cards(1)
        cards[0]["attributes"] = [{"name": "Длина", "value": "58 мм"}]
        prompts = []
        def answer(prompt, **kwargs):
            prompts.append(prompt)
            return {"grid": [{"c": 1, "r": 2, "v": "y"}]}, {}
        with patch("tenders.cascade._ai_json", side_effect=answer):
            self.cascade.step_6_agent_matrix(cards)
        self.assertIn("Проверить пункты: 2", prompts[0])
        self.assertEqual(cards[0]["matrix_status"], "complete")
        self.assertEqual(cards[0]["match_count"], 2)

    def test_cache_invalidates_when_source_or_selected_criteria_change(self):
        grid = [{"id": "*", "cells": {"1": "y", "2": "y"}}]
        self.grade(self.cards(1), grid)
        changed = self.cards(1)
        changed[0]["description"] = "Другая характеристика"
        self.assertEqual(self.grade(changed, grid).calls["step6"], 1)
        self.cascade.tz[0].value = "нет"
        self.assertEqual(self.grade(changed, grid).calls["step6"], 1)

    def test_deterministic_cells_survive_pending_tail(self):
        self.cascade.tz[0] = Criterion("Длина", "6 см", "длина", "=", "6 см", "см")
        cards = self.cards(1)
        cards[0]["attributes"] = [{"name": "Длина", "value": "8 см"}]
        with patch.dict("os.environ", {"CASCADE_STEP6_CEILING": "0"}):
            self.grade(cards, [])
        self.assertEqual(cards[0]["mismatch_count"], 1)
        self.assertEqual(cards[0]["matrix_status"], "pending")

    def test_another_variants_capacity_does_not_validate_the_face(self):
        self.cascade.tz = [Criterion("Ёмкость", "32 ГБ", "ёмкость", ">=", "32 ГБ", axis="capacity", num_min=Decimal(32768))]
        cards = [{"id": "A", "name": "Флешка 8 ГБ", "variants": [{"size": "32 ГБ"}]}]
        self.grade(cards, [])
        self.assertEqual(cards[0]["mismatch_count"], 1)

    def test_ambiguous_capacity_in_name_is_left_to_model(self):
        self.cascade.tz = [Criterion("Ёмкость", "32 ГБ", "ёмкость", ">=", "32 ГБ", axis="capacity", num_min=Decimal(32768))]
        cards = [{"id": "A", "name": "Память 8 ГБ / 32 ГБ"}]
        gateway = self.grade(cards, [])
        self.assertEqual(gateway.calls["step6"], 1)
        self.assertEqual(cards[0]["matrix_status"], "incomplete")


class CascadeBoundedIntegrationTests(TestCase):
    def test_flash_c1_is_first_and_keeps_16gb_variant(self):
        from .services import build_training_hypothesis

        _product("Флеш-карта USB 2.0 32 ГБ Флэш С1", external_id="F32", group_id="C1", price="491")
        _product("Флеш-карта USB 2.0 16 ГБ Флэш С1", external_id="F16", group_id="C1", price="410")
        _product("Флеш-карта USB 2.0 8 ГБ", external_id="F8", group_id="other", price="100")
        gateway = _Gateway(item="флеш-карта", queries=["флеш-карта", "usb"], criteria=[
            {"n": 1, "concept": "ёмкость", "operator": ">=", "value": "32 ГБ",
             "axis": "capacity", "num_min": 32768, "keep": True},
        ], grid=[{"id": "*", "cells": {"1": "y"}}])
        with patch("tenders.services._ai_gateway_json", side_effect=gateway):
            result = build_training_hypothesis(_line(rows=[{"label": "Ёмкость", "value": "не менее 32 ГБ"}]))
        card = result["catalog_candidates"][0]
        self.assertEqual(card["id"], "F32")
        self.assertIn("F16", card["variant_ids"])


class CascadeFeedbackCacheTests(TestCase):
    def setUp(self):
        super().setUp()
        self.cascade = Cascade(_line())
        self.cascade.feedback_instructions = [{"text": "предпочитай металл", "origin": "lesson", "lesson_id": 7}]
        self.response = {"instructions": [{"n": 1, "type": "priority", "criterion": "металл", "cards": [1], "applied": True}]}

    def classify(self, cards=None, response=None):
        cards = cards if cards is not None else [{"id": "A", "name": "Товар", "price": "10"}]
        with patch("tenders.cascade._ai_json", return_value=(response if response is not None else self.response, {})) as ai:
            self.cascade._classify_feedback(cards, [])
        return ai.call_count, cards

    def test_repeat_reapplies_cached_decisions(self):
        self.assertEqual(self.classify()[0], 1)
        calls, cards = self.classify()
        self.assertEqual(calls, 0)
        self.assertEqual(cards[0]["priority"], 0)

    def test_changed_lesson_and_changed_card_invalidate(self):
        self.classify()
        self.cascade.feedback_instructions[0]["text"] = "предпочитай пластик"
        self.assertEqual(self.classify()[0], 1)
        self.assertEqual(self.classify(cards=[{"id": "A", "name": "Другая карточка"}])[0], 1)

    def test_incomplete_response_is_retried(self):
        self.classify(response={"instructions": []})
        self.assertEqual(self.classify()[0], 1)

    def test_invalid_card_reference_is_not_cached(self):
        response = {"instructions": [{"n": 1, "type": "priority", "criterion": "металл", "cards": [99], "applied": True}]}
        self.classify(response=response)
        self.assertEqual(self.classify()[0], 1)

    def test_session_feedback_always_rechecks(self):
        self.cascade.feedback_instructions[0]["origin"] = "session"
        with patch("tenders.services._shortlist_card_images", return_value=([], [])):
            self.classify()
            self.assertEqual(self.classify()[0], 1)
