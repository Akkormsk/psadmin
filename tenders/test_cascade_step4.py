from unittest.mock import patch

from .cascade import Cascade
from .test_cascade import TestCase, _product
from .services import _run_name_filter_jev


class JevNameFilterServiceTests(TestCase):
    @patch("tenders.jev.decide_matrix")
    def test_jev_keeps_ambiguous_and_rejects_confident_non_items(self, decide):
        decide.return_value = (
            {"p1": {"noul": 0.95}, "p2": {"noul": 0.05}, "p3": {"noul": 0.5}},
            {"prompt_tokens": 30, "completion_tokens": 3},
        )
        usage = {}

        keep = _run_name_filter_jev(
            "футболка",
            [("shirt", "Футболка"), ("box", "Коробка для футболки"), ("maybe", "Набор футболка")],
            usage=usage,
        )

        self.assertEqual(keep, {"shirt", "maybe"})
        self.assertEqual(usage, {"prompt_tokens": 30, "completion_tokens": 3})


class JevNameFilterTests(TestCase):
    @patch("tenders.services._run_name_filter_jev")
    def test_step_4_can_use_jev_as_the_title_filter(self, run_jev):
        shirt = _product("Футболка", external_id="shirt")
        box = _product("Коробка для футболки", external_id="box")
        run_jev.return_value = {"shirt"}
        cascade = Cascade({"name": "Футболка"}, step_settings={"4": {"model": "jev", "cache": "no"}})
        cascade.item = "футболка"

        kept = cascade.step_4_name_filter([shirt, box])

        self.assertEqual([product.external_id for product in kept], ["shirt"])
        run_jev.assert_called_once()
        self.assertIn("jev-1.13.0", cascade.usage_by_model)
