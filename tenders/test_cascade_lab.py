import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .cascade import Cascade
from .models import CatalogProduct, CatalogSupplier, CascadeLabCase, CascadeLabRun, TenderEstimate, TenderLine


class CascadeLabViewTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("lab-admin", "admin@example.com", "password")
        self.user = get_user_model().objects.create_user("lab-user", password="password")
        estimate = TenderEstimate.objects.create(owner=self.admin, tender_number="T-1", name="Тест")
        self.line = TenderLine.objects.create(
            estimate=estimate, name="Флешка", quantity=100, nmck_unit=500,
            requirements={"requirements": [{"label": "Ёмкость", "value": "32 ГБ"}]},
        )

    def test_lab_is_available_only_to_superuser(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("cascade_lab")).status_code, 403)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("cascade_lab"))
        self.assertContains(response, "Лаборатория каскада")
        self.assertContains(response, "8. Цена и топ-10")
        self.assertContains(response, "Результат проверок")
        self.assertContains(response, "Минимум поисковых фраз")
        self.assertContains(response, "Максимум поисковых фраз")
        self.assertContains(response, "Карточек в первой проверке")
        self.assertContains(response, "Показать в результате")
        self.assertContains(response, "Агент разбора ТЗ")
        self.assertContains(response, "Агент чистки названия")
        self.assertContains(response, "GPT-4.1 nano · слабый")
        self.assertContains(response, "Claude Sonnet 4.5 · сильный")
        self.assertContains(response, "Каталоги для поиска")
        self.assertContains(response, "Интенсивность отсева")
        self.assertContains(response, "Требование к остатку")
        self.assertContains(response, "Агент матрицы и фидбека")
        self.assertContains(response, "Приоритет матрицы")
        self.assertContains(response, "Обновить цены перед показом")
        self.assertNotContains(response, "Параметры отдельных блоков, JSON")
        self.assertContains(response, 'id="lab-view-prev"')
        self.assertContains(response, 'id="lab-view-next"')
        self.assertContains(response, 'data-io-view="readable"', count=2)
        self.assertContains(response, 'data-io-view="json"', count=2)
        self.assertContains(response, 'id="lab-step-input-readable"')
        self.assertContains(response, 'id="lab-step-output-readable"')

    @patch("tenders.views._submit_cascade_lab")
    def test_create_run_from_readable_fields_and_step_controls(self, submit):
        self.client.force_login(self.admin)

        response = self.client.post(reverse("cascade_lab_run_create"), {
            "line_name": "Кружка",
            "line_quantity": "50",
            "requirement_label": ["Объём", "Цвет"],
            "requirement_value": ["не менее 300 мл", "белый"],
            "step_1_model": "openai/gpt-4.1-mini",
            "step_1_cache": "no",
            "step_2_min_phrases": "8",
            "step_2_max_phrases": "12",
            "step_2_model": "openai/gpt-4.1-nano",
            "step_2_cache": "no",
            "step_3_sources": "gifts",
            "step_4_model": "anthropic/claude-sonnet-4-5",
            "step_4_intensity": "strict",
            "step_4_cache": "no",
            "step_5_color_filter": "off",
            "step_5_stock_policy": "enough",
            "step_5_tolerance_percent": "5",
            "step_6_first_batch": "15",
            "step_6_ceiling": "60",
            "step_6_model": "openai/gpt-4.1-mini",
            "step_6_cache": "no",
            "step_6_numeric_prefill": "yes",
            "step_7_matrix_order": "yes_then_no",
            "step_7_price_order": "desc",
            "step_8_top": "10",
            "step_8_live_prices": "no",
            "stop_after": "3",
        })

        self.assertEqual(response.status_code, 202)
        run = CascadeLabRun.objects.get()
        self.assertEqual(run.input_payload["name"], "Кружка")
        self.assertEqual(run.input_payload["quantity"], "50")
        self.assertEqual(run.input_payload["requirements"]["requirements"][1], {"label": "Цвет", "value": "белый"})
        self.assertEqual(run.settings["steps"], {
            "1": {"model": "openai/gpt-4.1-mini", "cache": "no"},
            "2": {"min_phrases": 8, "max_phrases": 12, "model": "openai/gpt-4.1-nano", "cache": "no"},
            "3": {"sources": "gifts"},
            "4": {"model": "anthropic/claude-sonnet-4-5", "intensity": "strict", "cache": "no"},
            "5": {"color_filter": "off", "stock_policy": "enough", "tolerance_percent": 5},
            "6": {"first_batch": 15, "ceiling": 60, "model": "openai/gpt-4.1-mini", "cache": "no", "numeric_prefill": "yes"},
            "7": {"matrix_order": "yes_then_no", "price_order": "desc"},
            "8": {"live_prices": "no"},
        })
        self.assertEqual(run.settings["top"], 10)
        submit.assert_called_once_with(run.pk)

    def test_assistant_drawer_has_superuser_lab_button(self):
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse("tender_home")), "Открыть граф каскада")

        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("tender_home")), "Открыть граф каскада")

    @patch("tenders.views._submit_cascade_lab")
    def test_create_run_from_tender_line(self, submit):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("cascade_lab_run_create"), {
            "line_id": self.line.pk, "stop_after": "5",
        })
        self.assertEqual(response.status_code, 202)
        run = CascadeLabRun.objects.get()
        self.assertEqual(run.input_payload["name"], "Флешка")
        self.assertEqual(run.stop_after, 5)
        submit.assert_called_once_with(run.pk)

    @patch("tenders.views._submit_cascade_lab")
    def test_custom_cards_bypass_catalogue_and_can_be_saved_as_case(self, submit):
        self.client.force_login(self.admin)
        cards = [{"id": "manual-1", "name": "Флешка 32 ГБ", "price": "500"}]
        response = self.client.post(reverse("cascade_lab_run_create"), {
            "line_json": json.dumps({"name": "Флешка", "quantity": 100, "requirements": {"requirements": []}}),
            "cards_json": json.dumps(cards), "case_name": "Флешки вручную",
            "expectations_json": json.dumps({"must_include": ["manual-1"]}),
        })
        self.assertEqual(response.status_code, 202)
        run = CascadeLabRun.objects.get()
        self.assertEqual(run.current_step, 0)
        self.assertEqual(run.settings["custom_cards"], cards)
        self.assertEqual(CascadeLabCase.objects.get().custom_cards, cards)

    @patch("tenders.views._submit_cascade_lab")
    def test_saved_case_step_settings_can_be_changed_for_a_new_run(self, submit):
        case = CascadeLabCase.objects.create(
            name="Флешки", created_by=self.admin, input_payload={"name": "Флешка"},
            settings={"steps": {"2": {"max_phrases": 24}}},
        )
        self.client.force_login(self.admin)

        response = self.client.post(reverse("cascade_lab_run_create"), {
            "case_id": case.pk,
            "step_2_max_phrases": "8",
            "step_6_first_batch": "25",
            "step_6_ceiling": "75",
            "step_8_top": "10",
        })

        self.assertEqual(response.status_code, 202)
        self.assertEqual(CascadeLabRun.objects.get().settings["steps"]["2"]["max_phrases"], 8)
        submit.assert_called_once()

    def test_debugger_selects_the_step_being_executed(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "tenders" / "cascade_lab.js").read_text(encoding="utf-8")

        self.assertIn("selectedStep = target", script)
        self.assertIn("Выполняется…", script)
        self.assertIn("renderReadable", script)
        self.assertIn('localStorage.setItem(`cascade-lab-${side}-view`', script)
        self.assertIn('if (step === 2) return { name:', script)
        self.assertIn('item.status === "fallback" ? "is-error"', script)

    def test_run_detail_cannot_be_read_by_another_admin(self):
        other = get_user_model().objects.create_superuser("other-admin", "other@example.com", "password")
        run = CascadeLabRun.objects.create(created_by=self.admin, title="Чужой", input_payload={"name": "Товар"})
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("cascade_lab_run_detail", args=[run.pk])).status_code, 404)

    @patch("tenders.views._submit_cascade_lab")
    def test_paused_run_can_continue_by_one_step(self, submit):
        run = CascadeLabRun.objects.create(
            created_by=self.admin, title="Флешка", input_payload={"name": "Флешка"},
            current_step=2, stop_after=2, status="paused",
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("cascade_lab_run_execute", args=[run.pk]), {"stop_after": "3"},
        )

        self.assertEqual(response.status_code, 202)
        run.refresh_from_db()
        self.assertEqual(run.stop_after, 3)
        self.assertEqual(run.status, "running")
        submit.assert_called_once_with(run.pk)

    @patch("tenders.views._submit_cascade_lab")
    def test_fork_reuses_only_snapshots_before_selected_step(self, submit):
        source = CascadeLabRun.objects.create(
            created_by=self.admin, title="Флешка", input_payload={"name": "Флешка"},
            current_step=3, status="paused", snapshots=[
                {"step": number, "state": {"item": str(number)}, "metrics": {"seconds": 1, "cost_rub": 2}}
                for number in range(1, 4)
            ],
        )
        self.client.force_login(self.admin)
        response = self.client.post(reverse("cascade_lab_run_fork", args=[source.pk]), {"from_step": 3})
        self.assertEqual(response.status_code, 202)
        fork = CascadeLabRun.objects.exclude(pk=source.pk).get()
        self.assertEqual(fork.current_step, 2)
        self.assertEqual([item["step"] for item in fork.snapshots], [1, 2])
        self.assertEqual(fork.cascade_state["item"], "2")
        self.assertEqual(fork.total_cost_rub, 4)

    @patch("tenders.views._submit_cascade_lab")
    def test_fork_rejects_a_step_without_a_saved_input(self, submit):
        source = CascadeLabRun.objects.create(
            created_by=self.admin, title="Флешка", input_payload={"name": "Флешка"},
            current_step=2, status="paused",
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("cascade_lab_run_fork", args=[source.pk]), {"from_step": "5"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(CascadeLabRun.objects.count(), 1)
        submit.assert_not_called()


class CascadeLabRunnerTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("runner-admin", "runner@example.com", "password")
        self.run = CascadeLabRun.objects.create(
            created_by=self.admin, title="Любой товар",
            input_payload={"name": "Любой товар", "quantity": "10", "requirements": {"requirements": []}},
            stop_after=8,
        )

    def test_registry_has_exactly_the_eight_cascade_functions(self):
        from .cascade_lab import STEP_DEFINITIONS

        self.assertEqual([s["method"] for s in STEP_DEFINITIONS], sorted(
            name for name in vars(Cascade) if name.startswith("step_")
        ))

    @patch("tenders.cascade._cache_put")
    @patch("tenders.cascade._cache_get", return_value=None)
    @patch("tenders.cascade._ai_json")
    def test_step_2_cleans_name_with_its_own_agent(self, ai_json, cache_get, cache_put):
        ai_json.return_value = ({
            "item": "рубашка поло",
            "queries": ["рубашка поло", "поло", "polo shirt"],
        }, {"prompt_tokens": 20, "completion_tokens": 10})
        cascade = Cascade(
            {"name": "сувенир рубашка-поло с символикой Думы"},
            step_settings={"2": {
                "model": "openai/gpt-4.1-nano", "cache": "yes",
                "min_phrases": 4, "max_phrases": 12,
            }},
        )

        self.assertEqual(cascade.step_2_search_plan(), ["рубашка поло", "поло", "polo shirt"])
        self.assertEqual(cascade.item, "рубашка поло")
        self.assertEqual(ai_json.call_args.kwargs["model"], "openai/gpt-4.1-nano")
        self.assertIn("сувенир рубашка-поло с символикой Думы", ai_json.call_args.args[0])
        cache_get.assert_called_once()
        cache_put.assert_called_once()
        self.assertEqual(cascade.diagnostics["query_phrase_limits"], {
            "minimum": 4,
            "maximum": 12,
            "actual": 3,
            "minimum_met": False,
        })

    @patch("tenders.cascade._ai_json")
    @patch("tenders.cascade._cache_get", return_value=None)
    def test_step_1_only_parses_tz_and_can_use_selected_agent(self, cache_get, ai_json):
        ai_json.return_value = ({"item": "не должно использоваться", "queries": ["лишнее"], "criteria": []}, {})
        cascade = Cascade({
            "name": "Кружка",
            "requirements": {"requirements": [{"label": "Цвет", "value": "белый"}]},
        }, step_settings={
            "1": {"model": "anthropic/claude-haiku-4-5", "cache": "no"},
        })

        cascade.step_1_parse_tz()

        cache_get.assert_not_called()
        self.assertEqual(ai_json.call_args.kwargs["model"], "anthropic/claude-haiku-4-5")
        self.assertNotIn('"queries"', ai_json.call_args.args[0])
        self.assertEqual(cascade.item, "")
        self.assertEqual(cascade.queries, [])

    @patch("tenders.cascade._text_search_pool", return_value=[])
    def test_step_3_can_search_only_selected_supplier(self, search):
        CatalogSupplier.objects.create(code="oasis", name="Oasis", base_url="https://oasis.test")
        CatalogSupplier.objects.create(code="gifts", name="Gifts", base_url="https://gifts.test")
        cascade = Cascade({"name": "Кружка"}, step_settings={"3": {"sources": "gifts"}})

        cascade.step_3_search_by_name(["кружка"])

        search.assert_called_once_with("gifts", ["кружка"])

    @patch("tenders.services._run_name_filter", return_value={"1"})
    def test_step_4_passes_selected_agent_and_intensity_and_can_skip_cache(self, name_filter):
        product = SimpleNamespace(external_id="1", full_name="Кружка", name="Кружка")
        cascade = Cascade({"name": "Кружка"}, step_settings={
            "4": {"model": "anthropic/claude-sonnet-4-5", "intensity": "strict", "cache": "no"},
        })
        cascade.item = "кружка"

        self.assertEqual(cascade.step_4_name_filter([product]), [product])
        self.assertEqual(name_filter.call_args.kwargs["model"], "anthropic/claude-sonnet-4-5")
        self.assertEqual(name_filter.call_args.kwargs["intensity"], "strict")

    def test_step_5_can_require_enough_stock(self):
        supplier = CatalogSupplier.objects.create(code="gifts", name="Gifts", base_url="https://gifts.test")
        enough = CatalogProduct.objects.create(supplier=supplier, external_id="enough", name="Кружка", total_stock=100)
        short = CatalogProduct.objects.create(supplier=supplier, external_id="short", name="Кружка", total_stock=10)
        cascade = Cascade({"name": "Кружка", "quantity": 50}, step_settings={"5": {"stock_policy": "enough"}})

        cards = cascade.step_5_hard_gates_and_collapse([enough, short])

        self.assertEqual([card["id"] for card in cards], ["enough"])

    def test_step_5_applies_numeric_tolerance_when_choosing_variant(self):
        from .cascade import Criterion

        supplier = CatalogSupplier.objects.create(code="oasis", name="Oasis", base_url="https://oasis.test")
        near = CatalogProduct.objects.create(
            supplier=supplier, external_id="near", group_id="one", name="Флешка 31 ГБ",
            size="31 ГБ", price=Decimal("100"), total_stock=100,
        )
        exact = CatalogProduct.objects.create(
            supplier=supplier, external_id="exact", group_id="one", name="Флешка 32 ГБ",
            size="32 ГБ", price=Decimal("200"), total_stock=100,
        )
        cascade = Cascade({"name": "Флешка", "quantity": 10}, step_settings={"5": {"tolerance_percent": 5}})
        cascade.tz = [Criterion(
            label="Объём памяти", raw_value="не менее 32 ГБ", concept="Объём памяти",
            operator=">=", value="32", axis="capacity", num_min=Decimal("32768"),
        )]

        cards = cascade.step_5_hard_gates_and_collapse([near, exact])

        self.assertEqual(cards[0]["id"], "near")
        self.assertEqual(cards[0]["_axis_tolerance_percent"], 5)

    @patch.object(Cascade, "_grade_grid")
    def test_step_6_can_use_selected_agent(self, grade_grid):
        from .cascade import Criterion

        grade_grid.return_value = {"one": {1: ("y", "")}}
        cascade = Cascade({"name": "Кружка"}, step_settings={
            "6": {"model": "openai/gpt-4.1-mini", "cache": "no", "first_batch": 1, "ceiling": 1},
        })
        cascade._tz_hash = "tz"
        cascade.tz = [Criterion(label="Цвет", raw_value="белый", concept="Цвет", operator="=", value="белый")]
        card = {"id": "one", "name": "Кружка", "price": "100", "relevance": 0}

        cascade.step_6_agent_matrix([card])

        self.assertEqual(grade_grid.call_args.kwargs["model"], "openai/gpt-4.1-mini")

    @patch("tenders.services._shortlist_card_images", return_value=([], []))
    @patch("tenders.cascade._ai_json", return_value=({"instructions": []}, {}))
    def test_step_6_uses_selected_agent_for_feedback_too(self, ai_json, _images):
        cascade = Cascade({"name": "Кружка"}, step_settings={
            "6": {"model": "anthropic/claude-haiku-4-5"},
        })
        cascade.feedback_instructions = [{"text": "подними белые", "origin": "session"}]

        cascade._classify_feedback([{"id": "one", "name": "Кружка"}], [])

        self.assertEqual(ai_json.call_args.kwargs["model"], "anthropic/claude-haiku-4-5")

    def test_step_7_can_rank_yes_before_no_and_price_descending(self):
        cards = [
            {"id": "cheap", "name": "A", "article": "1", "price": "100", "match_count": 2, "mismatch_count": 0, "unknown_count": 0},
            {"id": "rich", "name": "B", "article": "2", "price": "300", "match_count": 5, "mismatch_count": 1, "unknown_count": 0},
            {"id": "mid", "name": "C", "article": "3", "price": "200", "match_count": 5, "mismatch_count": 1, "unknown_count": 0},
        ]
        cascade = Cascade({"name": "Товар"}, step_settings={"7": {"matrix_order": "yes_then_no", "price_order": "desc"}})

        ranked = cascade.step_7_collapse_and_sort(cards)

        self.assertEqual([card["id"] for card in ranked], ["rich", "mid", "cheap"])

    @patch("tenders.cascade._refresh_live_oasis_prices")
    def test_step_8_can_disable_live_price_refresh(self, refresh):
        cascade = Cascade({"name": "Товар"}, step_settings={"8": {"live_prices": "no"}})
        cascade._oasis_mirror = True

        self.assertEqual(cascade.step_8_price_and_top([{"id": "1"}]), [{"id": "1"}])
        refresh.assert_not_called()

    def test_runner_records_input_output_metrics_and_can_stop(self):
        from .cascade_lab import run_cascade_lab

        outputs = [[], ["товар"], [], [], [], [], [], []]
        patches = []
        for index, definition in enumerate(__import__("tenders.cascade_lab", fromlist=["STEP_DEFINITIONS"]).STEP_DEFINITIONS):
            patches.append(patch.object(Cascade, definition["method"], return_value=outputs[index]))
        for active in patches:
            active.start()
            self.addCleanup(active.stop)
        self.run.stop_after = 3
        self.run.save(update_fields=["stop_after"])

        run_cascade_lab(self.run.pk)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "paused")
        self.assertEqual(self.run.current_step, 3)
        self.assertEqual(len(self.run.snapshots), 3)
        self.assertIn("input", self.run.snapshots[0])
        self.assertIn("output", self.run.snapshots[0])
        self.assertIn("seconds", self.run.snapshots[0]["metrics"])
        self.assertIn("cost_rub", self.run.snapshots[0]["metrics"])
        self.assertEqual(self.run.snapshots[0]["input"], {"requirements": []})
        self.assertEqual(self.run.snapshots[1]["input"], {"name": "Любой товар"})

    def test_runner_marks_agent_fallback_in_snapshot(self):
        from .cascade_lab import run_cascade_lab

        def fallback(cascade):
            cascade.error = "Ответ агента не соответствует контракту"
            return []

        self.run.stop_after = 1
        self.run.save(update_fields=["stop_after"])
        with patch.object(Cascade, "step_1_parse_tz", fallback):
            run_cascade_lab(self.run.pk)

        self.run.refresh_from_db()
        self.assertEqual(self.run.snapshots[0]["status"], "fallback")
        self.assertEqual(self.run.snapshots[0]["error"], "Ответ агента не соответствует контракту")

    def test_criterion_numeric_bounds_are_json_serializable(self):
        from .cascade_lab import _json_value
        from .cascade import Criterion

        value = _json_value(Criterion(
            label="Ёмкость", raw_value="не менее 32 ГБ", concept="ёмкость",
            operator=">=", value="32 ГБ", num_min=Decimal("32768"),
        ))

        self.assertEqual(json.loads(json.dumps(value))["num_min"], "32768")

    def test_step_3_catalogue_snapshot_is_json_serializable(self):
        from .cascade_lab import _encode_output

        supplier = CatalogSupplier.objects.create(
            code="gifts", name="Gifts", base_url="https://example.com",
        )
        product = CatalogProduct.objects.create(
            supplier=supplier, external_id="flash-32", name="Флешка 32 ГБ",
            price=Decimal("499.90"), total_stock=20,
        )

        encoded = _encode_output([product])

        self.assertEqual(json.loads(json.dumps(encoded))["preview"][0]["price"], "499.90")

    def test_expectations_are_evaluated_on_final_cards(self):
        from .cascade_lab import evaluate_expectations

        result = evaluate_expectations(
            [{"id": "A", "mismatch_count": 0}, {"id": "B"}],
            {"must_include": ["A"], "must_exclude": ["X"], "ranks": {"B": {"min": 1, "max": 3}},
             "matrix": {"A": {"mismatch_count": 0}}},
            total_seconds=4, total_cost_rub=2,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["checks"]), 4)

    def test_runner_stops_before_next_step_when_resource_limit_is_reached(self):
        from .cascade_lab import run_cascade_lab

        self.run.settings = {"max_seconds": 0.000001}
        self.run.save(update_fields=["settings"])
        with patch.object(Cascade, "step_1_parse_tz", return_value=[]), patch.object(Cascade, "step_2_search_plan") as second:
            run_cascade_lab(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.current_step, 1)
        self.assertEqual(self.run.status, "paused")
        self.assertIn("лимит", self.run.result["pause_reason"].lower())
        second.assert_not_called()

    def test_preflight_failure_is_saved_as_run_error(self):
        from .cascade_lab import run_cascade_lab

        with patch("tenders.cascade_lab.preflight", side_effect=RuntimeError("Баланс недоступен")):
            with self.assertRaisesRegex(RuntimeError, "Баланс недоступен"):
                run_cascade_lab(self.run.pk)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "error")
        self.assertEqual(self.run.error, "Баланс недоступен")
        self.assertEqual(self.run.current_step, 0)
