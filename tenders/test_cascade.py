"""Тесты каскада подбора (tenders/cascade.py). Шлюз замокан — 0 обращений к ИИ.

Один роутер отвечает по маркеру в промпте: шаг 1 (разбор ТЗ), шаг 4 (фильтр
названий, из services._run_name_filter), шаг 6 (матрица агента)."""

import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from .cascade import Cascade
from .models import (
    CascadeCache, CatalogProduct, CatalogSupplier, Lesson, RequirementSkipRule,
)


def _supplier(code="oasis"):
    supplier, _ = CatalogSupplier.objects.get_or_create(
        code=code, defaults={"name": code.title(), "base_url": f"https://{code}.example"},
    )
    return supplier


def _product(name, *, external_id, group_id="", color_group_id="", price="100",
             stock=50, colors=None, description="", article="", size="", attributes=None,
             transit=0, on_order=False, supplier_code="oasis"):
    return CatalogProduct.objects.create(
        supplier=_supplier(supplier_code), external_id=external_id, article=article or external_id,
        group_id=group_id, color_group_id=color_group_id, name=name, full_name=name,
        description=description, size=size, colors=colors or [], attributes=attributes or [],
        price=Decimal(price), discount_price=None, total_stock=stock, stock_transit=transit,
        is_on_order=on_order, is_active=True,
    )


class _Gateway:
    """Роутер ответов ИИ по маркеру промпта. Потокобезопасен (чистая функция)."""

    def __init__(self, *, item="флешка", queries=None, criteria=None, grid=None, grid_cheap=None,
                 not_item=None, instructions=None, step1_error=False, step6_error=False):
        self.item = item
        self.queries = queries or ["флешка", "флеш-накопитель", "usb drive"]
        self.criteria = criteria or []
        self.grid = grid
        self.grid_cheap = grid_cheap  # если задан — ответ дешёвой модели отличается
        self.not_item = not_item or []
        self.instructions = instructions
        self.step1_error = step1_error
        self.step6_error = step6_error
        self.calls = {"step1": 0, "step4": 0, "step6": 0, "step6_cheap": 0, "step6_strong": 0}

    def __call__(self, prompt, **kwargs):
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        model = kwargs.get("model", "")
        if "разбираешь ТЗ тендера" in prompt:
            self.calls["step1"] += 1
            if self.step1_error:
                raise RuntimeError("boom")
            return {"item": self.item, "queries": self.queries, "criteria": self.criteria}, usage
        if "пронумерованный список названий товаров" in prompt:
            self.calls["step4"] += 1
            return {"not_item": self.not_item}, usage
        if "эксперт по подбору товара под тендер" in prompt:
            self.calls["step6"] += 1
            cheap = "haiku" in model
            self.calls["step6_cheap" if cheap else "step6_strong"] += 1
            if self.step6_error:
                raise RuntimeError("boom")
            body = {}
            grid = self.grid_cheap if (cheap and self.grid_cheap is not None) else self.grid
            if grid is not None:
                body["grid"] = self._grid_for(prompt, grid)
            if self.instructions is not None and not cheap:
                body["instructions"] = self.instructions
            return body, usage
        raise AssertionError(f"unexpected prompt: {prompt[:120]}")

    def _grid_for(self, prompt, grid):
        # промпт нумерует карточки «КАРТОЧКА <pos> | id <id>»; строим клетки по позиции
        import re

        positions = {int(m.group(1)): m.group(2) for m in re.finditer(r"КАРТОЧКА (\d+) \| id (\S+)", prompt)}
        default = next((c["cells"] for c in grid if c.get("id") == "*"), None)
        cells = []
        for pos, card_id in positions.items():
            spec = next((c["cells"] for c in grid if str(c.get("id")) == str(card_id)), default)
            for r, v in (spec or {}).items():
                cells.append({"c": pos, "r": int(r), "v": v})
        return cells


def _line(name="Флеш-накопитель", quantity="100", rows=None):
    return {
        "name": name, "quantity": quantity,
        "requirements": {"requirements": rows or []},
    }


def _run(gateway, line, **kwargs):
    with patch("tenders.services._ai_gateway_json", side_effect=gateway):
        return Cascade(line, **kwargs).run()


class CascadeStep1Tests(TestCase):
    def test_parses_criteria_and_caches_by_tz_hash(self):
        rows = [{"label": "Интерфейс", "value": "USB 2.0"}, {"label": "Ёмкость", "value": "не менее 32 ГБ"}]
        gw = _Gateway(criteria=[
            {"n": 1, "concept": "интерфейс", "operator": "=", "value": "USB 2.0", "keep": True},
            {"n": 2, "concept": "ёмкость памяти", "operator": ">=", "value": "32 ГБ", "unit": "ГБ",
             "keep": True, "axis": "capacity", "num_min": 32768},
        ])
        result = _run(gw, _line(rows=rows))
        self.assertEqual([c.concept for c in result.tz], ["интерфейс", "ёмкость памяти"])
        self.assertEqual(result.tz[1].num_min, Decimal("32768"))
        self.assertEqual(result.tz[1].axis, "capacity")
        self.assertEqual(gw.calls["step1"], 1)
        self.assertTrue(CascadeCache.objects.filter(kind="tz").exists())

        # второй прогон того же ТЗ — вызова шага 1 нет
        gw2 = _Gateway(criteria=[])
        _run(gw2, _line(rows=rows))
        self.assertEqual(gw2.calls["step1"], 0)

    def test_marking_row_is_unchecked_by_the_model(self):
        rows = [{"label": "Маркировка", "value": "Честный Знак"}]
        gw = _Gateway(criteria=[{"n": 1, "concept": "маркировка", "operator": "=",
                                 "value": "Честный Знак", "keep": False}])
        result = _run(gw, _line(rows=rows))
        self.assertFalse(result.tz[0].checked)
        self.assertEqual([r for r in result.requirement_selection if r["selected"]], [])

    def test_saved_skip_rule_overrides_model_keep(self):
        RequirementSkipRule.objects.create(label="Гарантия", label_normalized="гарантия")
        rows = [{"label": "Гарантия", "value": "12 месяцев"}]
        gw = _Gateway(criteria=[{"n": 1, "concept": "гарантия", "operator": "=",
                                 "value": "12 месяцев", "keep": True}])
        result = _run(gw, _line(rows=rows), skip_labels={"гарантия"})
        self.assertFalse(result.tz[0].checked)

    def test_client_selected_flag_wins_over_model(self):
        rows = [{"label": "Упаковка", "value": "блистер", "selected": True}]
        gw = _Gateway(criteria=[{"n": 1, "concept": "упаковка", "operator": "=",
                                 "value": "блистер", "keep": False}])
        result = _run(gw, _line(rows=rows))
        self.assertTrue(result.tz[0].checked)

    def test_step1_failure_falls_back_to_raw_rows(self):
        rows = [{"label": "Цвет", "value": "синий"}]
        gw = _Gateway(step1_error=True)
        result = _run(gw, _line(rows=rows))
        self.assertEqual(result.tz[0].value, "синий")
        self.assertTrue(result.error)


class CascadeSearchTests(TestCase):
    def test_step3_matches_product_name_not_description(self):
        _product("Ежедневник Бизнес", external_id="A1", description="в комплекте флешка-закладка")
        _product("USB-флешка Твист", external_id="A2")
        gw = _Gateway(queries=["флешка"], criteria=[])
        result = _run(gw, _line())
        ids = {c["id"] for c in result.candidates}
        self.assertIn("A2", ids)
        self.assertNotIn("A1", ids)

    def test_step4_drops_the_names_the_model_rejects(self):
        _product("USB-флешка Твист", external_id="A2")
        _product("Коробка для флешки", external_id="A3")
        gw = _Gateway(queries=["флешка"], criteria=[], not_item=[2])
        result = _run(gw, _line())
        ids = {c["id"] for c in result.candidates}
        self.assertEqual(ids, {"A2"})
        self.assertEqual(gw.calls["step4"], 1)


class CascadeHardGateTests(TestCase):
    def _colour_line(self):
        return _line(rows=[{"label": "Цвет", "value": "синий"}])

    def _colour_gateway(self, grid=None):
        return _Gateway(
            queries=["кружка"],
            criteria=[{"n": 1, "concept": "цвет", "operator": "=", "value": "синий", "keep": True}],
            grid=grid,
        )

    def test_colour_family_conflict_is_rejected(self):
        _product("Кружка керамическая", external_id="C1", colors=["красный"])
        _product("Кружка керамическая", external_id="C2", colors=["синий"])
        result = _run(self._colour_gateway(grid=[]), self._colour_line())
        self.assertEqual({c["id"] for c in result.candidates}, {"C2"})

    def test_adjacent_shade_is_kept_for_the_agent(self):
        _product("Кружка керамическая", external_id="C3", colors=["голубой"])
        result = _run(self._colour_gateway(grid=[]), self._colour_line())
        self.assertEqual({c["id"] for c in result.candidates}, {"C3"})

    def test_stock_shortage_is_kept_zero_everything_is_dropped(self):
        _product("Кружка A", external_id="S1", stock=1)
        _product("Кружка B", external_id="S2", stock=0, transit=0, on_order=False)
        gw = _Gateway(queries=["кружка"], criteria=[], grid=[])
        result = _run(gw, _line(name="Кружка", quantity="500"))
        self.assertEqual({c["id"] for c in result.candidates}, {"S1"})


class CascadeCollapseTests(TestCase):
    def test_collapse_keeps_the_variant_that_fits_the_capacity_tz(self):
        for cap, eid, price in [("16", "F16", "410"), ("32", "F32", "491"), ("64", "F64", "690")]:
            _product(f"USB-флешка Твист {cap} ГБ", external_id=eid, group_id="G1", price=price)
        gw = _Gateway(
            queries=["флешка"],
            criteria=[{"n": 1, "concept": "ёмкость памяти", "operator": ">=", "value": "32 ГБ",
                       "unit": "ГБ", "keep": True, "axis": "capacity", "num_min": 32768}],
            grid=[{"id": "F32", "cells": {"1": "y"}}],
        )
        result = _run(gw, _line(rows=[{"label": "Ёмкость", "value": "не менее 32 ГБ"}]))
        self.assertEqual(len(result.candidates), 1)
        face = result.candidates[0]
        self.assertIn("32", face["name"])
        sizes = {v.get("size") for v in face["variants"]}
        self.assertTrue({"16 ГБ", "64 ГБ"} & sizes or "64" in " ".join(sizes))

    def test_agent_brief_lists_the_variants(self):
        for cap, eid in [("16", "F16"), ("32", "F32")]:
            _product(f"USB-флешка {cap} ГБ", external_id=eid, group_id="G1")
        cascade = Cascade(_line())
        cascade.tz = []
        card = cascade._serialize(
            CatalogProduct.objects.get(external_id="F32"),
            list(CatalogProduct.objects.filter(group_id="G1")),
        )
        self.assertIn("Варианты", cascade._card_brief(card))


class CascadeAgentTests(TestCase):
    def _line2(self):
        return _line(name="Флешка", rows=[
            {"label": "Интерфейс", "value": "USB 2.0"},
            {"label": "Ёмкость", "value": "не менее 32 ГБ"},
        ])

    def _gw(self, grid, **kw):
        return _Gateway(queries=["флешка"], criteria=[
            {"n": 1, "concept": "интерфейс", "operator": "=", "value": "USB 2.0", "keep": True},
            {"n": 2, "concept": "ёмкость", "operator": ">=", "value": "32 ГБ", "keep": True},
        ], grid=grid, **kw)

    def test_missing_cell_is_unknown_not_a_pass(self):
        _product("Флешка Твист 8 ГБ", external_id="P1")
        gw = self._gw(grid=[{"id": "P1", "cells": {"1": "y"}}])  # пункт 2 пропущен
        result = _run(gw, self._line2())
        card = result.candidates[0]
        self.assertEqual(card["mismatch_count"], 0)
        self.assertEqual(card["unknown_count"], 1)
        self.assertNotEqual(card["fit"], "exact")

    def test_a_fully_ungraded_card_never_ranks_as_exact(self):
        _product("Флешка A", external_id="P1")
        _product("Флешка B", external_id="P2")
        gw = self._gw(grid=[{"id": "P1", "cells": {"1": "y", "2": "y"}}], step6_error=False)
        # ответим только по P1: у P2 клеток нет вовсе
        result = _run(gw, self._line2())
        by_id = {c["id"]: c for c in result.candidates}
        self.assertEqual(by_id["P1"]["fit"], "exact")
        self.assertNotEqual(by_id["P2"]["fit"], "exact")
        self.assertEqual(by_id["P2"]["unknown_count"], 2)
        self.assertEqual(result.candidates[0]["id"], "P1")  # gradedкарточка впереди

    def test_verdict_cache_skips_the_agent_on_a_repeat(self):
        _product("Флешка Твист 64 ГБ", external_id="P1")
        grid = [{"id": "P1", "cells": {"1": "y", "2": "y"}}]
        first = _run(self._gw(grid=grid), self._line2())
        self.assertEqual(first.candidates[0]["fit"], "exact")
        self.assertTrue(CascadeCache.objects.filter(kind="verdict").exists())

        gw2 = self._gw(grid=grid)
        second = _run(gw2, self._line2())
        self.assertEqual(gw2.calls["step6"], 0)
        self.assertEqual(second.candidates[0]["fit"], "exact")
        self.assertGreaterEqual(second.diagnostics["verdict_cache_hits"], 1)


class CascadeSortTests(TestCase):
    def test_fixed_key_orders_by_mismatch_then_match_then_unknown(self):
        for eid in ("M0", "M1", "U1"):
            _product(f"Флешка {eid}", external_id=eid)
        gw = _Gateway(queries=["флешка"], criteria=[
            {"n": 1, "concept": "интерфейс", "operator": "=", "value": "USB 2.0", "keep": True},
            {"n": 2, "concept": "ёмкость", "operator": ">=", "value": "32 ГБ", "keep": True},
        ], grid=[
            {"id": "M0", "cells": {"1": "y", "2": "y"}},
            {"id": "U1", "cells": {"1": "y", "2": "m"}},
            {"id": "M1", "cells": {"1": "y", "2": "n"}},
        ])
        result = _run(gw, _line(name="Флешка", rows=[
            {"label": "Интерфейс", "value": "USB 2.0"}, {"label": "Ёмкость", "value": "32 ГБ"},
        ]))
        self.assertEqual([c["id"] for c in result.candidates], ["M0", "U1", "M1"])

    def test_no_cap_before_the_agent(self):
        for i in range(120):
            _product(f"Флешка {i:03d}", external_id=f"N{i:03d}", group_id=f"G{i:03d}")
        gw = _Gateway(queries=["флешка"], criteria=[], grid=[])
        result = _run(gw, _line())
        self.assertEqual(result.diagnostics["groups"], 120)
        self.assertEqual(len(result.candidates), 10)  # показываем 10, но отобраны из всех 120


class CascadeFeedbackTests(TestCase):
    def test_priority_instruction_raises_a_card(self):
        _product("Флешка обычная", external_id="P1")
        _product("Флешка премиум", external_id="P2")
        gw = _Gateway(queries=["флешка"], criteria=[], grid=[],
                      instructions=[{"n": 1, "type": "priority", "criterion": "премиум", "cards": ["P2"]}])
        result = _run(gw, _line(), session_feedback=[{"text": "нужны премиум", "scope": "catalog"}])
        self.assertEqual(result.candidates[0]["id"], "P2")
        self.assertEqual(result.instructions[0]["type"], "priority")
        self.assertTrue(result.instructions[0]["applied"])

    def test_exclude_instruction_removes_a_card(self):
        _product("Флешка обычная", external_id="P1")
        _product("Флешка с колпачком", external_id="P2")
        gw = _Gateway(queries=["флешка"], criteria=[], grid=[],
                      instructions=[{"n": 1, "type": "exclude", "criterion": "с колпачком", "cards": ["P2"]}])
        result = _run(gw, _line(), session_feedback=[{"text": "убери с колпачком", "scope": "catalog"}])
        ids = {c["id"] for c in result.candidates}
        self.assertEqual(ids, {"P1"})
        self.assertEqual(result.removed[0]["id"], "P2")

    def test_verdict_cache_is_written_even_when_a_lesson_fires(self):
        # регресс: раньше проход с уроком не писал кэш вердикта, и позиции с
        # уроком (почти все реальные) никогда не ускорялись
        _product("Флешка Твист 64 ГБ", external_id="P1")
        rows = [{"label": "Ёмкость", "value": "не менее 32 ГБ"}]
        crit = [{"n": 1, "concept": "ёмкость", "operator": ">=", "value": "32 ГБ", "keep": True}]
        gw1 = _Gateway(queries=["флешка"], criteria=crit,
                       grid=[{"id": "P1", "cells": {"1": "y"}}], instructions=[])
        _run(gw1, _line(rows=rows), lessons_provider=lambda i, l: [{"id": 3, "instruction": "не детские"}])
        self.assertTrue(CascadeCache.objects.filter(kind="verdict").exists())

        gw2 = _Gateway(queries=["флешка"], criteria=crit,
                       grid=[{"id": "P1", "cells": {"1": "y"}}], instructions=[])
        result = _run(gw2, _line(rows=rows), lessons_provider=lambda i, l: [{"id": 3, "instruction": "не детские"}])
        # матрица из кэша — grid-проход не гонялся; сработал только разбор фидбека
        self.assertGreaterEqual(result.diagnostics["verdict_cache_hits"], 1)
        self.assertEqual(result.candidates[0]["fit"], "exact")

    def test_a_lesson_is_folded_in_as_an_instruction(self):
        _product("Флешка детская", external_id="P1")
        gw = _Gateway(queries=["флешка"], criteria=[], grid=[], instructions=[])
        result = _run(
            gw, _line(),
            lessons_provider=lambda item, labels: [{"id": 7, "instruction": "не бери детские"}],
        )
        self.assertEqual(gw.calls["step6"], 1)  # урок запустил проход агента
        self.assertEqual(result.instructions[0]["lesson_id"], 7)


class CascadeTwoTierTests(TestCase):
    def _many(self, n):
        for i in range(n):
            _product(f"Флешка {i:03d} 32 ГБ", external_id=f"N{i:03d}", group_id=f"G{i:03d}")

    def _tz(self):
        return _line(name="Флешка", rows=[{"label": "Ёмкость", "value": "не менее 32 ГБ"}])

    def _crit(self):
        return [{"n": 1, "concept": "ёмкость", "operator": ">=", "value": "32 ГБ", "keep": True}]

    def test_two_tier_splits_cheap_all_strong_subset(self):
        self._many(40)
        gw = _Gateway(queries=["флешка"], criteria=self._crit(), grid=[{"id": "*", "cells": {"1": "y"}}])
        result = _run(gw, self._tz())
        d = result.diagnostics["two_tier"]
        self.assertEqual(d["cheap"], 40)
        self.assertLessEqual(d["strong"], 30)          # сильная модель — не по всем
        self.assertGreater(gw.calls["step6_cheap"], 0)
        self.assertGreater(gw.calls["step6_strong"], 0)

    def test_flag_off_is_single_tier(self):
        self._many(40)
        gw = _Gateway(queries=["флешка"], criteria=self._crit(), grid=[{"id": "*", "cells": {"1": "y"}}])
        with patch.dict("os.environ", {"CASCADE_TWO_TIER": "0"}):
            result = _run(gw, self._tz())
        self.assertEqual(gw.calls["step6_cheap"], 0)
        self.assertNotIn("two_tier", result.diagnostics)

    def test_small_pool_stays_single_tier(self):
        self._many(5)
        gw = _Gateway(queries=["флешка"], criteria=self._crit(), grid=[{"id": "*", "cells": {"1": "y"}}])
        result = _run(gw, self._tz())
        self.assertEqual(gw.calls["step6_cheap"], 0)

    def test_strong_grid_overrides_cheap_for_top_cards(self):
        _product("Флешка TOP 32 ГБ", external_id="TOP", group_id="GT", price="1")
        for i in range(24):
            _product(f"Флешка {i:02d} 32 ГБ", external_id=f"N{i:02d}", group_id=f"G{i:02d}", price="500")
        # дешёвая модель считает TOP лучшей (y), остальных — мимо (n).
        # сильная перепроверяет TOP и говорит n.
        gw = _Gateway(
            queries=["флешка"], criteria=self._crit(),
            grid=[{"id": "*", "cells": {"1": "n"}}],
            grid_cheap=[{"id": "TOP", "cells": {"1": "y"}}, {"id": "*", "cells": {"1": "n"}}],
        )
        result = _run(gw, self._tz())
        top = result.candidates[0]
        self.assertEqual(top["id"], "TOP")            # дешёвая вывела её вперёд
        self.assertEqual(top["mismatch_count"], 1)     # но сильная перепроверила → n


class CascadeAxisPrefillTests(TestCase):
    def test_code_marks_the_capacity_row_itself_and_overrides_the_model(self):
        _product("Флешка Твист 8 ГБ", external_id="P1")
        gw = _Gateway(
            queries=["флешка"],
            criteria=[{"n": 1, "concept": "ёмкость", "operator": ">=", "value": "32 ГБ",
                       "keep": True, "axis": "capacity", "num_min": 32768}],
            grid=[{"id": "P1", "cells": {"1": "y"}}],   # модель ошибочно говорит "подходит"
        )
        result = _run(gw, _line(name="Флешка", rows=[{"label": "Ёмкость", "value": "не менее 32 ГБ"}]))
        card = result.candidates[0]
        self.assertEqual(card["mismatch_count"], 1)   # код поставил ✗ по 8 ГБ vs ≥32
        self.assertIn("8", card["mismatches"][0])

    def test_silent_card_is_left_to_the_agent(self):
        _product("Флешка Твист", external_id="P1")   # ёмкости в названии нет
        gw = _Gateway(
            queries=["флешка"],
            criteria=[{"n": 1, "concept": "ёмкость", "operator": ">=", "value": "32 ГБ",
                       "keep": True, "axis": "capacity", "num_min": 32768}],
            grid=[{"id": "P1", "cells": {"1": "m"}}],
        )
        result = _run(gw, _line(name="Флешка", rows=[{"label": "Ёмкость", "value": "не менее 32 ГБ"}]))
        self.assertEqual(result.candidates[0]["unknown_count"], 1)


class CascadeNameFilterCacheTests(TestCase):
    def test_second_run_uses_the_cached_keep_set(self):
        _product("USB-флешка Твист", external_id="A2")
        _product("Коробка для флешки", external_id="A3")
        gw1 = _Gateway(queries=["флешка"], criteria=[], not_item=[2], grid=[])
        _run(gw1, _line())
        self.assertEqual(gw1.calls["step4"], 1)
        self.assertTrue(CascadeCache.objects.filter(kind="namefilter").exists())

        gw2 = _Gateway(queries=["флешка"], criteria=[], not_item=[2], grid=[])
        result = _run(gw2, _line())
        self.assertEqual(gw2.calls["step4"], 0)
        self.assertEqual({c["id"] for c in result.candidates}, {"A2"})


class BuildHypothesisIntegrationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="a", password="p")

    def test_end_to_end_through_build_training_hypothesis(self):
        from .services import build_training_hypothesis

        _product("USB-флешка Твист 32 ГБ", external_id="F32", group_id="G1", price="491")
        _product("USB-флешка Твист 16 ГБ", external_id="F16", group_id="G1", price="410")
        gw = _Gateway(
            item="флешка", queries=["флешка", "флеш-накопитель"],
            criteria=[{"n": 1, "concept": "ёмкость памяти", "operator": ">=", "value": "32 ГБ",
                       "keep": True, "axis": "capacity", "num_min": 32768}],
            grid=[{"id": "F32", "cells": {"1": "y"}}],
        )
        line = _line(name="Поставка флеш-накопителей с логотипом", rows=[
            {"label": "Ёмкость", "value": "не менее 32 ГБ"},
        ])
        with patch("tenders.services._ai_gateway_json", side_effect=gw):
            hypothesis = build_training_hypothesis(line)
        self.assertEqual(hypothesis["search_plan"]["item"], "флешка")
        self.assertTrue(hypothesis["catalog_candidates"])
        self.assertIn("32", hypothesis["catalog_candidates"][0]["name"])
        self.assertEqual(
            [r["label"] for r in hypothesis["requirement_selection"]], ["Ёмкость"],
        )
        self.assertEqual(hypothesis["catalog_intent"]["synonyms"][:1], ["флешка"])
