import json
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .cascade import Cascade
from .cascade_lab import _decode_output, _encode_output, execute_cascade_steps
from .models import CascadeConfigVersion, CascadeLabPreset, CatalogProduct, CatalogSupplier, TenderEstimate, TenderLine


class CascadeLabViewTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("lab-admin", "admin@example.com", "password")
        self.user = get_user_model().objects.create_user("lab-user", password="password")
        estimate = TenderEstimate.objects.create(owner=self.admin, tender_number="T-1", name="Тест")
        self.line = TenderLine.objects.create(
            estimate=estimate, name="Флешка", quantity=100, nmck_unit=500,
            requirements={"requirements": [{"label": "Ёмкость", "value": "32 ГБ"}]},
        )

    def test_lab_has_presets_without_saved_run_history(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("cascade_lab"))
        self.assertContains(response, "Лаборатория каскада")
        self.assertContains(response, "Максимум активных требований")
        self.assertContains(response, "Карточек в одной волне")
        self.assertContains(response, "Максимум новых карточек для ИИ")
        self.assertContains(response, "Применить настройки к подбору товаров")
        script = (Path(__file__).resolve().parents[1] / "static" / "tenders" / "cascade_lab.js").read_text(encoding="utf-8")
        self.assertIn('if (step !== 8 || side !== "output" || !row.url) return name;', script)
        self.assertIn("for (let step = fromStep; step <= stopAfter; step += 1)", script)
        self.assertIn('body.set("stop_after", String(step));', script)
        self.assertIn("const resume = history.length ? [{...history[history.length - 1], input: null}] : [];", script)
        self.assertIn('body.set("snapshots", JSON.stringify(resume));', script)
        self.assertIn("render(data);", script)
        self.assertNotContains(response, "Последние прогоны")
        self.assertNotContains(response, "Сохранить как тест")

    def test_lab_is_available_only_to_superuser(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("cascade_lab")).status_code, 403)

    def test_named_preset_is_created_then_overwritten(self):
        self.client.force_login(self.admin)
        url = reverse("cascade_lab_preset_save")
        first = self.client.post(url, {"name": "Быстро", "settings": json.dumps({"top": 10})})
        second = self.client.post(url, {"name": "Быстро", "settings": json.dumps({"top": 15})})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(CascadeLabPreset.objects.count(), 1)
        self.assertEqual(CascadeLabPreset.objects.get().settings["top"], 15)

    @patch("tenders.views.execute_cascade_steps")
    def test_execute_uses_explicit_target_step(self, execute):
        execute.return_value = {"status": "paused", "current_step": 6, "snapshots": []}
        self.client.force_login(self.admin)
        response = self.client.post(reverse("cascade_lab_execute"), {
            "line_id": self.line.pk,
            "from_step": 1,
            "stop_after": 6,
            "prior_total_seconds": "4.25",
            "prior_total_cost_rub": "1.75",
            "settings": json.dumps({"steps": {}}),
            "snapshots": "[]",
            "cascade_state": "{}",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(execute.call_args.kwargs["stop_after"], 6)
        self.assertEqual(execute.call_args.kwargs["prior_total_seconds"], 4.25)
        self.assertEqual(execute.call_args.kwargs["prior_total_cost_rub"], 1.75)

    def test_activate_settings_creates_global_active_version(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("cascade_lab_activate"), {
            "name": "Проверено на флешках",
            "settings": json.dumps({"steps": {"8": {"top": 10}}}),
        })

        self.assertEqual(response.status_code, 200)
        active = CascadeConfigVersion.objects.get(is_active=True)
        self.assertEqual(active.name, "Проверено на флешках")

    @patch("tenders.cascade._ai_json")
    @patch("tenders.cascade_lab.preflight")
    def test_projected_cost_limit_stops_before_next_step(self, _preflight, gateway):
        result = execute_cascade_steps(
            line={
                "name": "Флешка",
                "quantity": "100",
                "requirements": {"requirements": [{"label": "Ёмкость", "value": "32 ГБ"}]},
            },
            settings={"steps": {}, "max_cost_rub": 0.0001, "max_seconds": 10},
            stop_after=2,
        )

        gateway.assert_not_called()
        self.assertEqual(result["current_step"], 1)
        self.assertIn("лимит", result["result"]["pause_reason"].lower())

    def test_catalog_family_variants_survive_step_by_step_resume(self):
        supplier = CatalogSupplier.objects.create(code="test", name="Test")
        parent = CatalogProduct.objects.create(
            supplier=supplier, external_id="16", name="Флешка 16 ГБ",
            family_key="test:flash", variant_axes={"capacity_mb": 16384},
        )
        child = CatalogProduct.objects.create(
            supplier=supplier, external_id="32", name="Флешка 32 ГБ",
            family_key="test:flash", variant_axes={"capacity_mb": 32768},
        )

        restored = _decode_output(_encode_output([parent]))

        self.assertEqual(
            {product.pk for product in restored[0]._variant_products},
            {parent.pk, child.pk},
        )

    @patch("tenders.cascade._refresh_live_oasis_prices")
    @patch("tenders.cascade.OasisClient")
    def test_step_8_live_prices_use_one_short_attempt(self, oasis_client, refresh):
        cascade = Cascade({"name": "Поло", "quantity": 10})
        cascade._oasis_mirror = True
        cascade.deadline = time.perf_counter() + 4
        cards = [{"external_id": "1", "supplier_code": "oasis", "name": "Поло"}]

        self.assertEqual(cascade.step_8_price_and_top(cards), cards)

        kwargs = oasis_client.call_args.kwargs
        self.assertEqual(kwargs["max_attempts"], 1)
        self.assertEqual(kwargs["min_interval"], 0)
        self.assertGreaterEqual(kwargs["timeout"], 1)
        self.assertLessEqual(kwargs["timeout"], 4)
        refresh.assert_called_once_with(oasis_client.return_value, cards, quantity=10)

    @patch("tenders.cascade_lab.preflight")
    def test_step_7_output_drops_detailed_matrix_before_step_8(self, _preflight):
        card = {
            "id": "polo-1", "name": "Поло", "match_count": 1,
            "mismatch_count": 0, "unknown_count": 0,
            "matrix": [{"criterion": "Цвет", "verdict": "yes"}],
        }
        result = execute_cascade_steps(
            line={"name": "Поло", "quantity": 10},
            settings={"steps": {}, "max_cost_rub": 10, "max_seconds": 10},
            from_step=7,
            stop_after=7,
            snapshots=[{"step": 6, "output": [card], "state": {}, "metrics": {}}],
        )

        output = result["snapshots"][-1]["output"]
        self.assertNotIn("matrix", output[0])
        self.assertEqual(output[0]["match_count"], 1)

    @patch("tenders.cascade_lab.preflight")
    def test_polo_sized_step_7_payload_reaches_step_8(self, _preflight):
        cards = [
            {
                "id": f"polo-{index}", "name": f"Поло {index}", "price": str(500 + index),
                "match_count": 20, "mismatch_count": 0, "unknown_count": 0,
                "matrix": [
                    {"criterion": f"Параметр {criterion}", "verdict": "yes", "reason": "Подтверждено карточкой"}
                    for criterion in range(20)
                ],
            }
            for index in range(507)
        ]
        step_7 = execute_cascade_steps(
            line={"name": "Поло", "quantity": 10},
            settings={"steps": {"8": {"live_prices": "no"}}, "max_cost_rub": 10, "max_seconds": 10},
            from_step=7,
            stop_after=7,
            snapshots=[{"step": 6, "output": cards, "state": {}, "metrics": {}}],
        )["snapshots"][-1]
        self.client.force_login(self.admin)

        response = self.client.post(reverse("cascade_lab_execute"), {
            "line_id": self.line.pk,
            "from_step": 8,
            "stop_after": 8,
            "settings": json.dumps({"steps": {"8": {"live_prices": "no"}}, "top": 10}),
            "snapshots": json.dumps([step_7]),
            "cascade_state": "{}",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current_step"], 8)
        self.assertEqual(len(response.json()["snapshots"][-1]["output"]), 10)
