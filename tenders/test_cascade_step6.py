"""Контракт шага 6: полнота ответа, кэш и границы с шагом 5."""

from decimal import Decimal
from unittest.mock import patch

from .cascade import Cascade, Criterion
from .models import CascadeCache
from .test_cascade import TestCase, _Gateway, _line, _product


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
        # Шаг 5 обычно инициализирует matrix (и, если включено, прикрывает
        # часть клеток кодом) до того, как карточка попадёт на шаг 6 — эти
        # тесты проверяют шаг 6 в изоляции, поэтому сами кладут пустую
        # (полностью открытую) матрицу, как будто шаг 5 ничего не решил.
        _checked, rows = self.cascade._checked_rows()
        for card in cards:
            if "matrix" not in card:
                self.cascade._init_unknown(card, rows)
        gateway = _Gateway(grid=grid)
        with patch("tenders.services._ai_gateway_json", side_effect=gateway):
            self.cascade.step_6_agent_matrix(cards)
        return gateway

    def test_every_card_without_a_step_5_answer_is_graded_in_one_pass(self):
        cards = self.cards(40)
        gateway = self.grade(cards, [{"id": "*", "cells": {"1": "y", "2": "y"}}])
        self.assertEqual(gateway.calls["step6"], 14)  # ceil(40/3) пачек, один проход
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 40)
        self.assertEqual(CascadeCache.objects.filter(kind="verdict").count(), 40)
        self.assertTrue(all(card["matrix_status"] == "complete" for card in cards))

    def test_matrix_keeps_per_criterion_verdict_reason_and_source(self):
        cards = self.cards(1)
        self.grade(cards, [{"id": "*", "cells": {"1": "y", "2": "m"}}])

        self.assertEqual([cell["verdict"] for cell in cards[0]["matrix"]], ["yes", "unknown"])
        self.assertTrue(all(cell["criterion"] for cell in cards[0]["matrix"]))
        self.assertTrue(all(cell["source"] == "agent" for cell in cards[0]["matrix"]))

    def test_complete_unknowns_are_cached(self):
        self.grade(self.cards(10), [{"id": "*", "cells": {"1": "m", "2": "m"}}])
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 10)
        self.assertEqual(CascadeCache.objects.filter(kind="verdict").count(), 10)

    def test_missing_cells_are_not_cached_or_counted_as_complete(self):
        cards = self.cards(10)
        self.grade(cards, [{"id": "*", "cells": {"1": "y"}}])
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 10)
        self.assertFalse(CascadeCache.objects.filter(kind="verdict").exists())
        self.assertTrue(all(c["matrix_status"] == "incomplete" for c in cards))

    def test_a_cell_step_5_already_closed_needs_no_agent_call(self):
        # Прикрытая шагом 5 клетка (source="code") приходит на шаг 6 уже
        # решённой — сама детерминированная проверка живёт в шаге 5
        # (_prefill_card), здесь важно только то, что шаг 6 её не трогает
        # и не тратит на неё вызов агента.
        self.cascade.tz = [Criterion(
            label="Ёмкость", raw_value="32 ГБ", concept="ёмкость", operator=">=",
            value="32 ГБ", unit="ГБ", axis="capacity", num_min=Decimal(32),
        )]
        checked, rows = self.cascade._checked_rows()
        card = {"id": "A", "name": "Флешка 32 ГБ", "price": "100", "relevance": 0}
        self.cascade._init_unknown(card, rows)
        self.cascade._apply_cell(card, rows, 1, "y", "по варианту", "code")
        self.cascade._recompute_card_summary(card)
        gateway = self.grade([card], [])
        self.assertEqual(gateway.calls["step6"], 0)
        self.assertEqual(card["matrix_status"], "complete")
        self.assertEqual(card["fit"], "exact")
        self.assertEqual(card["matrix"][0]["source"], "code")
        # Чисто кодовое решение не кэшируется — пересчитать его бесплатно,
        # кэш нужен только чтобы не звать агента повторно.
        self.assertFalse(CascadeCache.objects.filter(kind="verdict").exists())

    def test_repeat_uses_cached_verdicts_without_calling_the_agent_again(self):
        grid = [{"id": "*", "cells": {"1": "y", "2": "y"}}]
        self.grade(self.cards(5), grid)
        gateway = self.grade(self.cards(5), grid)
        self.assertEqual(gateway.calls["step6"], 0)
        self.assertEqual(self.cascade.diagnostics["step6"]["cached"], 5)
        self.assertEqual(self.cascade.diagnostics["step6"]["graded"], 0)

    def test_agent_cache_is_not_reused_by_the_jev_agent_ladder(self):
        # Баг, найденный живым прогоном 2026-09-28 (cascade_compare_result.json):
        # без отдельного тега в ключе кэша второй прогон с engine="jev_agent"
        # молча читал вердикты, посчитанные обычным агентом, и лесенка ни разу
        # реально не отрабатывала на закэшированных карточках.
        grid = [{"id": "*", "cells": {"1": "y", "2": "y"}}]
        self.grade(self.cards(3), grid)  # обычный агент — кладёт кэш под свой ключ

        cards = self.cards(3)
        _checked, rows = self.cascade._checked_rows()
        for card in cards:
            self.cascade._init_unknown(card, rows)
        self.cascade.step_settings = {"6": {"engine": "jev_agent", "cache": "yes"}}

        with patch("tenders.jev.decide_matrix", return_value=({}, {})) as mock_jev, \
             patch("tenders.services._ai_gateway_json") as mock_agent:
            mock_agent.return_value = ({"grid": [{"c": 1, "r": 1, "v": "y"}, {"c": 1, "r": 2, "v": "y"}]}, {})
            self.cascade.step_6_agent_matrix(cards)

        mock_jev.assert_called()  # не взято из кэша обычного агента

    def test_incomplete_legacy_cache_is_retried(self):
        from .cascade import _cache_put

        _cache_put("verdict", "bounded-test|0", {"grid": {"1": ["y", ""]}})
        gateway = self.grade(self.cards(1), [{"id": "*", "cells": {"1": "y", "2": "y"}}])
        self.assertEqual(gateway.calls["step6"], 1)

    def test_failed_gateway_leaves_cards_ungraded_without_caching(self):
        cards = self.cards(10)
        _checked, rows = self.cascade._checked_rows()
        for card in cards:
            self.cascade._init_unknown(card, rows)
        gateway = _Gateway(step6_error=True)
        with patch("tenders.services._ai_gateway_json", side_effect=gateway):
            self.cascade.step_6_agent_matrix(cards)
        self.assertEqual(gateway.calls["step6"], 4)  # ceil(10/3) пачек, все провалились
        self.assertFalse(CascadeCache.objects.filter(kind="verdict").exists())
        self.assertTrue(self.cascade.error)

    def test_jev_grades_only_open_cells_and_keeps_uncertain_cells_unknown(self):
        cards = self.cards(2)
        _checked, rows = self.cascade._checked_rows()
        for card in cards:
            self.cascade._init_unknown(card, rows)
        self.cascade._apply_cell(cards[0], rows, 1, "y", "закрыто кодом", "code")
        self.cascade._recompute_card_summary(cards[0])
        self.cascade.step_settings = {"6": {"engine": "jev", "cache": "no"}}

        captured = {}

        def jev(state, questions, **_kwargs):
            captured["state"], captured["questions"] = state, questions
            return {
                "c1r2": {"noul": 0.1},
                "c2r1": {"noul": 0.9},
                "c2r2": {"noul": 0.5},
            }, {"prompt_tokens": 12, "completion_tokens": 3}

        with patch("tenders.jev.decide_matrix", side_effect=jev):
            self.cascade.step_6_agent_matrix(cards)

        self.assertNotIn("c1r1", captured["questions"])
        self.assertEqual(set(captured["questions"]), {"c1r2", "c2r1", "c2r2"})
        self.assertEqual([cell["verdict"] for cell in cards[0]["matrix"]], ["yes", "no"])
        self.assertEqual([cell["verdict"] for cell in cards[1]["matrix"]], ["yes", "unknown"])
        self.assertEqual(cards[1]["matrix"][1]["reason"], "Jev: недостаточная уверенность")
        self.assertEqual(self.cascade.usage_by_model["jev-1.13.0"]["prompt_tokens"], 12)

    def test_jev_agent_ladder_only_sends_the_agent_what_jev_was_unsure_about(self):
        # Строка 1 — Джев уверен («y»), строка 2 — Джев не уверен («m»,
        # вероятность около середины). Ожидание: строка 1 закрывается
        # Джевом и остаётся такой, даже если агент (мок) тоже прислал по
        # ней ответ — агент реально нужен только для строки 2.
        cards = self.cards(1)
        _checked, rows = self.cascade._checked_rows()
        self.cascade._init_unknown(cards[0], rows)
        self.cascade.step_settings = {"6": {"engine": "jev_agent", "cache": "no"}}

        def jev(state, questions, **_kwargs):
            return {"c1r1": {"noul": 0.95}, "c1r2": {"noul": 0.5}}, {"prompt_tokens": 4, "completion_tokens": 1}

        gateway = _Gateway(grid=[{"id": "*", "cells": {"1": "n", "2": "y"}}])
        with patch("tenders.jev.decide_matrix", side_effect=jev), patch("tenders.services._ai_gateway_json", side_effect=gateway):
            self.cascade.step_6_agent_matrix(cards)

        self.assertEqual(cards[0]["matrix"][0]["source"], "jev")
        self.assertEqual(cards[0]["matrix"][0]["verdict"], "yes")  # Джев сказал "y" — агентовский "n" не применился
        self.assertEqual(cards[0]["matrix"][1]["source"], "agent")
        self.assertEqual(cards[0]["matrix"][1]["verdict"], "yes")
        self.assertEqual(self.cascade.diagnostics["step6"]["jev_prefilled_cells"], 1)
        self.assertEqual(gateway.calls["step6"], 1)

    def test_jev_agent_ladder_skips_the_agent_entirely_when_jev_is_sure_about_everything(self):
        cards = self.cards(1)
        _checked, rows = self.cascade._checked_rows()
        self.cascade._init_unknown(cards[0], rows)
        self.cascade.step_settings = {"6": {"engine": "jev_agent", "cache": "no"}}

        def jev(state, questions, **_kwargs):
            return {"c1r1": {"noul": 0.9}, "c1r2": {"noul": 0.1}}, {"prompt_tokens": 4, "completion_tokens": 1}

        gateway = _Gateway(grid=[])
        with patch("tenders.jev.decide_matrix", side_effect=jev), patch("tenders.services._ai_gateway_json", side_effect=gateway):
            self.cascade.step_6_agent_matrix(cards)

        self.assertTrue(all(cell["source"] == "jev" for cell in cards[0]["matrix"]))
        self.assertEqual(cards[0]["matrix_status"], "complete")
        self.assertEqual(gateway.calls["step6"], 0)  # агент не вызывается вовсе — Джев закрыл все клетки сам

    def test_jev_failure_leaves_cards_ungraded_without_caching(self):
        cards = self.cards(1)
        _checked, rows = self.cascade._checked_rows()
        self.cascade._init_unknown(cards[0], rows)
        self.cascade.step_settings = {"6": {"engine": "jev", "cache": "no"}}

        with patch("tenders.jev.decide_matrix", side_effect=RuntimeError("offline")):
            self.cascade.step_6_agent_matrix(cards)

        self.assertEqual(cards[0]["matrix_status"], "pending")
        self.assertTrue(self.cascade.error)
        self.assertFalse(CascadeCache.objects.filter(kind="verdict").exists())


class CascadeJevTriageTests(TestCase):
    """Большой Джев (`_jev_triage`) — отдельный переключатель между шагом 5
    и шагом 6, не часть настроек шага 6. Смотри вопрос-на-карточку, не
    вопрос-на-клетку: он никогда не проставляет вердикты по параметрам,
    только решает, идёт ли карточка в шаг 6 вообще."""

    def setUp(self):
        self.cascade = Cascade(_line())
        self.cascade._tz_hash = "triage-test"
        self.cascade.tz = [Criterion(label="Свойство А", raw_value="да", concept="Свойство А", operator="~", value="да")]

    def card(self, card_id="A"):
        card = {"id": card_id, "name": f"Товар {card_id}", "price": "100", "relevance": 0}
        _checked, rows = self.cascade._checked_rows()
        self.cascade._init_unknown(card, rows)
        return card

    def test_off_by_default_does_not_call_jev_at_all(self):
        cards = [self.card()]
        with patch("tenders.jev.decide_matrix") as mock_jev:
            result = self.cascade._jev_triage(cards)
        mock_jev.assert_not_called()
        self.assertIs(result, cards)
        self.assertFalse(cards[0].get("_removed"))

    def test_confident_no_removes_the_card_before_step_6(self):
        cards = [self.card("A"), self.card("B")]
        with patch("tenders.jev.decide_matrix", return_value=({"c1": {"noul": 0.05}, "c2": {"noul": 0.9}}, {})):
            self.cascade.step_settings = {"triage": {"engine": "jev"}}
            self.cascade._jev_triage(cards)
        self.assertTrue(cards[0]["_removed"])
        self.assertIn("Джев-триаж", cards[0]["_removed_reason"])
        self.assertFalse(cards[1].get("_removed"))
        self.assertEqual(self.cascade.diagnostics["jev_triage"], {"checked": 2, "dropped": 1})

    def test_confident_yes_and_unsure_never_get_a_fabricated_verdict(self):
        # Ключевой инвариант (см. docs/assistant_protocol.md §10.4): триаж
        # никогда не заполняет матрицу сам, даже при полной уверенности "да".
        cards = [self.card("A"), self.card("B")]
        with patch("tenders.jev.decide_matrix", return_value=({"c1": {"noul": 0.95}, "c2": {"noul": 0.5}}, {})):
            self.cascade.step_settings = {"triage": {"engine": "jev"}}
            self.cascade._jev_triage(cards)
        for card in cards:
            self.assertFalse(card.get("_removed"))
            self.assertTrue(all(cell["source"] == "not_checked" for cell in card["matrix"]))

    def test_dropped_card_is_skipped_by_step_6_entirely(self):
        cards = [self.card("A")]
        self.cascade.step_settings = {"triage": {"engine": "jev"}, "6": {"cache": "no"}}
        with patch("tenders.jev.decide_matrix", return_value=({"c1": {"noul": 0.01}}, {})):
            self.cascade._jev_triage(cards)
        gateway = _Gateway(grid=[{"id": "*", "cells": {"1": "y"}}])
        with patch("tenders.services._ai_gateway_json", side_effect=gateway):
            self.cascade.step_6_agent_matrix(cards)
        self.assertEqual(gateway.calls["step6"], 0)
        self.assertTrue(cards[0]["_removed"])

    def test_already_disqualified_or_complete_cards_are_not_asked_about(self):
        settled_by_code = self.card("A")
        _checked, rows = self.cascade._checked_rows()
        self.cascade._apply_cell(settled_by_code, rows, 1, "n", "код уже решил", "code")
        self.cascade._recompute_card_summary(settled_by_code)
        cards = [settled_by_code]
        with patch("tenders.jev.decide_matrix") as mock_jev:
            self.cascade.step_settings = {"triage": {"engine": "jev"}}
            self.cascade._jev_triage(cards)
        mock_jev.assert_not_called()


class CascadeBoundedIntegrationTests(TestCase):
    def test_flash_c1_is_first_and_keeps_16gb_variant(self):
        from .services import build_training_hypothesis

        _product("Флеш-карта USB 2.0 32 ГБ Флэш С1", external_id="F32", group_id="C1", price="491",
                  attributes=[{"name": "Объем памяти", "value": "32 ГБ"}])
        _product("Флеш-карта USB 2.0 16 ГБ Флэш С1", external_id="F16", group_id="C1", price="410",
                  attributes=[{"name": "Объем памяти", "value": "16 ГБ"}])
        _product("Флеш-карта USB 2.0 8 ГБ", external_id="F8", group_id="other", price="100",
                  attributes=[{"name": "Объем памяти", "value": "8 ГБ"}])
        gateway = _Gateway(item="флеш-карта", queries=["флеш-карта", "usb"], criteria=[
            {"n": 1, "concept": "объём памяти", "operator": ">=", "value": "32 ГБ",
             "unit": "ГБ", "axis": "capacity", "num_min": 32, "keep": True},
        ], grid=[{"id": "*", "cells": {"1": "y"}}])
        with patch("tenders.services._ai_gateway_json", side_effect=gateway):
            result = build_training_hypothesis(_line(rows=[{"label": "Объём памяти", "value": "не менее 32 ГБ"}]), current={"route": {"steps": ["Закупка готового изделия"], "processes": [{"id": "purchase", "kind": "catalog", "name": "Закупка готового изделия"}]}}, recompute="catalog")
        card = result["catalog_candidates"][0]
        self.assertEqual(card["id"], "F32")
        self.assertIn("F16", card["variant_ids"])
