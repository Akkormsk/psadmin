"""Шаг 7 проверяется на готовых матрицах без БД и моделей."""

from copy import deepcopy

from django.test import SimpleTestCase

from .cascade import Cascade


class CascadeVerifiedSortTests(SimpleTestCase):
    def setUp(self):
        self.cascade = Cascade({"name": "Произвольный товар"})

    def card(self, identifier, status, *, mismatches=0, matches=0, priority=1, price="100"):
        return {
            "id": identifier, "name": identifier, "matrix_status": status,
            "mismatch_count": mismatches, "match_count": matches,
            "unknown_count": 10 - mismatches - matches, "priority": priority,
            "relevance": 0, "price": price,
        }

    def test_verified_mismatch_precedes_unverified_zero_mismatches(self):
        cards = [self.card("pending", "pending", price="1"),
                 self.card("partial-answer", "incomplete", matches=5),
                 self.card("verified", "complete", mismatches=1, matches=9)]
        before = deepcopy(cards)
        ranked = self.cascade.step_7_collapse_and_sort(cards)
        self.assertEqual(ranked[0]["id"], "verified")
        self.assertEqual(cards, before)
        self.assertEqual(len(ranked), 3)

    def test_manual_priority_still_precedes_verification(self):
        cards = [self.card("verified", "complete", matches=10),
                 self.card("raised", "pending", priority=0)]
        ranked = self.cascade.step_7_collapse_and_sort(cards)
        self.assertEqual(ranked[0]["id"], "raised")

    def test_existing_matrix_order_and_price_remain_unchanged(self):
        cards = [self.card("mismatch", "complete", mismatches=1, matches=9, price="1"),
                 self.card("unknown", "complete", matches=9, price="2"),
                 self.card("expensive", "complete", matches=10, price="200"),
                 self.card("cheap", "complete", matches=10, price="100")]
        ranked = self.cascade.step_7_collapse_and_sort(cards)
        self.assertEqual([c["id"] for c in ranked], ["cheap", "expensive", "unknown", "mismatch"])

    def test_missing_status_keeps_compatibility_with_old_step6(self):
        cards = [self.card("expensive", "complete", matches=10, price="200"),
                 self.card("cheap", "complete", matches=10, price="100")]
        for card in cards:
            del card["matrix_status"]
        ranked = self.cascade.step_7_collapse_and_sort(cards)
        self.assertEqual([c["id"] for c in ranked], ["cheap", "expensive"])
