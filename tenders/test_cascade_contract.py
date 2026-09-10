"""Восемь независимых точек входа и последовательная передача результата."""

from contextlib import ExitStack
from unittest.mock import patch

from django.test import SimpleTestCase

from .cascade import Cascade


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
