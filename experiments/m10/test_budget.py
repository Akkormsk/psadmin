import unittest
from decimal import Decimal

from experiments.m10.budget import Budget, MAX_CALLS_PER_ATTEMPT, MAX_REQUEST_BYTES


MODEL = "openai/gpt-4.1"
RATES = {MODEL: {"input_rub_per_million": 270, "output_rub_per_million": 1080}}


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.body = {"model": MODEL, "messages": [], "tools": [], "max_tokens": 1200,
            "tool_choice": "required", "parallel_tool_calls": False}

    def test_reservation_blocks_before_network(self):
        budget = Budget(1, RATES)
        with self.assertRaisesRegex(RuntimeError, "budget"):
            budget.reserve("A", self.body)
        self.assertEqual(budget.reservations, [])

    def test_missing_usage_keeps_reservation(self):
        budget = Budget(100, RATES)
        index = budget.reserve("A", self.body)
        reserved = budget.charged_or_reserved
        for usage in (None, {}, {"prompt_tokens": 100}, {"prompt_tokens": 100, "completion_tokens": None}):
            budget.settle(index, MODEL, usage)
            self.assertEqual(budget.charged_or_reserved, reserved)

    def test_cache_is_not_counted_twice_or_discounted_for_budget(self):
        budget = Budget(100, RATES)
        index = budget.reserve("A", self.body)
        budget.settle(index, MODEL, {"prompt_tokens": 1000, "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 900}})
        self.assertEqual(budget.charged_or_reserved, Decimal("0.378"))

    def test_attempt_limit_counts_reserved_failures(self):
        budget = Budget(100, RATES)
        for _ in range(MAX_CALLS_PER_ATTEMPT):
            budget.reserve("A", self.body)
        with self.assertRaisesRegex(RuntimeError, "request limit"):
            budget.reserve("A", self.body)

    def test_large_request_is_stopped_without_truncation(self):
        budget = Budget(100, RATES)
        self.body["messages"] = [{"role": "user", "content": "x" * MAX_REQUEST_BYTES}]
        with self.assertRaisesRegex(RuntimeError, "envelope"):
            budget.reserve("A", self.body)

    def test_unicode_escapes_do_not_count_as_provider_input(self):
        budget = Budget(10, RATES)
        self.body["messages"] = [{"role": "user", "content": "я" * 5000}]
        index = budget.reserve("A", self.body)
        self.assertLess(budget.reservations[index], 10)
        self.assertGreater(budget.reservations[index], Decimal("1.35"))

    def test_unexpected_usage_is_charged_and_stops(self):
        budget = Budget(100, RATES)
        index = budget.reserve("A", self.body)
        with self.assertRaisesRegex(RuntimeError, "reconcile"):
            budget.settle(index, MODEL, {"prompt_tokens": 1000000, "completion_tokens": 1200})
        self.assertGreater(budget.charged_or_reserved, 100)

    def test_invalid_budget_and_changed_contract_are_rejected(self):
        for limit in (0, -1, "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                Budget(limit, RATES)
        budget = Budget(100, RATES)
        for key, value in [("max_tokens", 2000), ("tool_choice", "auto"), ("parallel_tool_calls", True)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                budget.reserve("A", {**self.body, key: value})


if __name__ == "__main__":
    unittest.main()
