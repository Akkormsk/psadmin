"""Восемь независимых точек входа и последовательная передача результата."""

from contextlib import ExitStack
from unittest.mock import patch

from django.test import SimpleTestCase

from .cascade import Cascade
from .cascade import Criterion


class CascadeContractTests(SimpleTestCase):
    def test_run_calls_exactly_eight_steps_in_order(self):
        names = [
            "step_1_parse_tz", "step_2_search_plan", "step_3_search_by_name",
            "step_4_name_filter", "step_5_hard_gates_and_collapse",
            "step_6_agent_matrix", "step_7_collapse_and_sort", "step_8_price_and_top",
        ]
        self.assertEqual(sorted(n for n in vars(Cascade) if n.startswith("step_")), names)
        cascade = Cascade({"name": "Произвольный товар"})
        outputs, calls = [[] for _ in names], []

        def step(index):
            def invoke(*args):
                calls.append(names[index])
                if index >= 2:
                    self.assertIs(args[0], outputs[index - 1])
                else:
                    self.assertEqual(args, ())
                return outputs[index]
            return invoke

        with ExitStack() as stack:
            for index, name in enumerate(names):
                stack.enter_context(patch.object(cascade, name, side_effect=step(index)))
            result = cascade.run()
        self.assertEqual(calls, names)
        self.assertIs(result.candidates, outputs[7])
        self.assertEqual(
            set(result.diagnostics["seconds"]),
            {"1", "2", "3", "4", "5", "6_jev", "6", "7", "8"},
        )

    def test_step_1_limit_keeps_only_most_important_non_explicit_criteria(self):
        cascade = Cascade({"name": "Товар"}, step_settings={"1": {"max_active_requirements": 2}})
        rows = [
            {"label": "Цвет", "value": "синий"},
            {"label": "Совместимость", "value": "USB-C"},
            {"label": "Материал", "value": "металл"},
        ]
        payload = {"criteria": [
            {"label": "Цвет", "raw_value": "синий", "concept": "цвет", "value": "синий", "importance": 30},
            {"label": "Совместимость", "raw_value": "USB-C", "concept": "совместимость", "value": "USB-C", "importance": 100},
            {"label": "Материал", "raw_value": "металл", "concept": "материал", "value": "металл", "importance": 70},
        ]}

        cascade._load_step1(payload, rows)

        self.assertEqual([c.label for c in cascade.tz if c.checked], ["Совместимость", "Материал"])
        self.assertEqual(len(cascade.tz), 3)

    def test_step_1_limit_grows_by_rows_added_through_splitting(self):
        """Строка ТЗ «Размер, мм: 40 х 50», разбитая на «Длина»+«Ширина», не
        должна вытеснять соседа лимитом — лишняя строка, появившаяся из
        разбиения (2 родилось из 1), поднимает лимит на столько же."""
        cascade = Cascade({"name": "Товар"}, step_settings={"1": {"max_active_requirements": 2}})
        rows = [
            {"label": "Цвет", "value": "синий"},
            {"label": "Размер, мм", "value": "40 х 50"},
        ]
        payload = {"criteria": [
            {"label": "Цвет", "source_label": "Цвет", "raw_value": "синий", "concept": "цвет",
             "value": "синий", "importance": 90},
            {"label": "Длина", "source_label": "Размер, мм", "raw_value": "40 х 50", "concept": "длина",
             "value": "40", "num_min": "40", "importance": 80},
            {"label": "Ширина", "source_label": "Размер, мм", "raw_value": "40 х 50", "concept": "ширина",
             "value": "50", "num_min": "50", "importance": 75},
        ]}

        cascade._load_step1(payload, rows)

        self.assertEqual(len(cascade.tz), 3)
        self.assertEqual([c.label for c in cascade.tz if c.checked], ["Цвет", "Длина", "Ширина"])
        self.assertEqual(cascade.diagnostics["active_requirement_limit"], 3)
