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
        self.assertEqual(gateway.calls["step6"], 9)
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

    def test_matrix_keeps_per_criterion_verdict_reason_and_source(self):
        cards = self.cards(1)
        self.grade(cards, [{"id": "*", "cells": {"1": "y", "2": "m"}}])

        self.assertEqual([cell["verdict"] for cell in cards[0]["matrix"]], ["yes", "unknown"])
        self.assertTrue(all(cell["criterion"] for cell in cards[0]["matrix"]))
        self.assertTrue(all(cell["source"] == "agent" for cell in cards[0]["matrix"]))

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

    def test_axis_prefill_completes_clear_cell_without_model_answer(self):
        self.cascade.tz = [Criterion(
            label="Ёмкость", raw_value="32 ГБ", concept="ёмкость", operator=">=",
            value="32 ГБ", axis="capacity", num_min=Decimal(32768),
        )]
        cards = [{"id": "A", "name": "Флешка 32 ГБ", "price": "100", "relevance": 0}]
        self.grade(cards, [])
        self.assertEqual(cards[0]["matrix_status"], "complete")
        self.assertEqual(cards[0]["fit"], "exact")
        self.assertEqual(cards[0]["matrix"][0]["source"], "code")
        self.assertTrue(CascadeCache.objects.filter(kind="verdict").exists())

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
            _cache_put("verdict", f"bounded-test|{i}", {"grid": {"1": ["y", ""], "2": ["y", ""]}})
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
        self.assertEqual(gateway.calls["step6"], 9)
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
