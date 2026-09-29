import copy
import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Lesson, ProcessDefinition, ProductionTrainingExample, ProductionTrainingSession
from .services import TenderAIError, apply_catalog_candidate, build_training_hypothesis


class RouteTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="route-admin", password="test")
        self.client.force_login(self.user)
        self.purchase_process = ProcessDefinition.objects.get_or_create(name="Закупка готового изделия", role="supply")[0]
        self.print_process = ProcessDefinition.objects.get_or_create(name="Тиснение", role="production")[0]
        self.turnkey_process = ProcessDefinition.objects.get_or_create(name="Изготовление под ключ", role="production")[0]
        self.own_process = ProcessDefinition.objects.get_or_create(name="Своё производство", role="production")[0]
        self.box_process = ProcessDefinition.objects.get_or_create(name="Закупка коробки", role="supply")[0]
        self.line = {"name": "Пакет", "quantity": "100", "requirements": {"requirements": [
            {"label": "Нанесение", "value": "тиснение", "selected": False},
        ]}}
        self.route = {"reason": "Готовый пакет и отдельное нанесение", "processes": [
            {"id": "purchase", "process_id": str(self.purchase_process.pk), "kind": "catalog", "details": []},
            {"id": "print", "process_id": str(self.print_process.pk), "kind": "production", "details": []},
        ]}
        self.answer = {"route": self.route, "item": "пакет", "understood_changes": [], "questions": []}
        self.ai = self.enterContext(patch("tenders.services._ai_gateway_json", return_value=(self.answer, {"prompt_tokens": 20, "completion_tokens": 30})))
        self.enterContext(patch("tenders.gateway_budget.preflight"))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("Tests must not access the network")))
        self.cascade = self.enterContext(patch("tenders.cascade.Cascade.run"))

    def build(self, **kwargs):
        return build_training_hypothesis(copy.deepcopy(self.line), **kwargs)

    def test_route_build_does_not_start_catalog_and_keeps_full_tz(self):
        result = self.build()
        self.cascade.assert_not_called()
        self.assertFalse(result["catalog_search_started"])
        self.assertEqual(result["route"]["steps"], ["Закупка готового изделия", "Тиснение"])
        self.assertIn("тиснение", self.ai.call_args.args[0])
        self.assertEqual(result["costs"], [])

    def test_missing_required_parameter_becomes_a_question(self):
        self.print_process.parameters = {"required": ["код клише", "тираж"], "optional": []}
        self.print_process.save(update_fields=["parameters"])
        result = self.build()
        texts = [q["text"] for q in result["questions"]]
        self.assertIn("код клише", texts)
        self.assertNotIn("тираж", texts)  # уже известен из quantity позиции

    def test_required_parameter_already_covered_by_tz_is_not_asked(self):
        self.print_process.parameters = {"required": ["тиснение"], "optional": []}
        self.print_process.save(update_fields=["parameters"])
        result = self.build()  # ТЗ уже содержит строку "Нанесение: тиснение"
        self.assertEqual(result["questions"], [])

    def test_catalog_only_route_mode_skips_route_agent(self):
        with patch.dict(os.environ, {"ROUTE_MODE": "catalog_only"}):
            result = self.build()
        self.ai.assert_not_called()
        self.assertEqual(result["route"]["steps"], ["Закупка готового изделия"])
        self.assertEqual(result["route"]["processes"][0]["kind"], "catalog")
        self.assertEqual(result["route_mode"], "catalog_only")

    def test_route_preview_starts_catalog_in_the_same_job(self):
        route = {"route": {"processes": [{"id": "catalog-only", "kind": "catalog"}]}}
        catalog = {"route": route["route"], "catalog_search_started": True}
        with patch("tenders.views._submit_assistant_job") as submit, patch(
            "tenders.views.build_training_hypothesis", side_effect=[route, catalog]
        ) as build:
            response = self.client.post(
                reverse("tender_production_route_preview"),
                {"line_json": json.dumps(self.line), "start_catalog": "1"},
            )
            self.assertEqual(response.status_code, 202)
            session = ProductionTrainingSession.objects.get()
            submit.call_args.args[1](session)
        self.assertEqual(build.call_count, 2)
        self.assertEqual(build.call_args_list[1].kwargs["recompute"], "catalog")
        self.assertEqual(build.call_args_list[1].kwargs["step_id"], "catalog-only")

    def test_explicit_catalog_search_preserves_route(self):
        from .cascade import CascadeResult
        current = self.build()
        self.cascade.return_value = CascadeResult(item="пакет", queries=[], tz=[], candidates=[], catalog_intent={}, requirement_selection=[])
        result = self.build(current=current, recompute="catalog", step_id="purchase")
        self.cascade.assert_called_once()
        self.assertEqual(result["route"], current["route"])
        self.assertTrue(result["catalog_search_started"])
        self.assertEqual(self.ai.call_count, 1)

    def test_catalog_cannot_start_for_production_step(self):
        current = self.build()
        with self.assertRaises(TenderAIError):
            self.build(current=current, recompute="catalog", step_id="print")
        self.cascade.assert_not_called()

    def test_selecting_product_does_not_replace_route(self):
        current = self.build()
        current["catalog_candidates"] = [{"id": "x", "name": "Пакет", "price": "10", "supplier_name": "Тест", "fit": "exact"}]
        result = apply_catalog_candidate(current, self.line, "x")
        self.assertEqual(result["route"], current["route"])

    def test_route_feedback_does_not_search_and_is_recalled_after_confirmation(self):
        current = self.build()
        self.answer["route"] = {"reason": "По указанию администратора", "processes": [
            {"id": "turnkey", "process_id": str(self.turnkey_process.pk), "kind": "production", "details": ["Подрядчик А"]},
        ]}
        self.answer["understood_changes"] = ["Пакет изготавливаем под ключ"]
        revised = self.build(current=current, feedback="Такие пакеты заказываем под ключ у подрядчика А")
        self.cascade.assert_not_called()
        session = ProductionTrainingSession.objects.create(created_by=self.user, position_name="Пакет", requirements=self.line["requirements"], current_hypothesis=revised)
        response = self.client.post(reverse("tender_confirm_production_type"), {"payload": json.dumps({"session_id": session.pk, "line": self.line})})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(Lesson.objects.filter(scope="route", is_active=True).exists())
        session.refresh_from_db()
        self.assertIsNotNone(session.confirmed_example_id)
        self.build()
        prompt = self.ai.call_args.args[0]
        self.assertIn("подрядчика А", prompt)
        self.assertIn("Изготовление под ключ", prompt)

    def test_step_feedback_keeps_its_target(self):
        result = self.build(current=self.build(), feedback="Тиснение у подрядчика Б", step_id="print")
        instruction = result["session_instructions"][-1]
        self.assertEqual(instruction["scope"], "production_step")
        self.assertEqual(instruction["step_id"], "print")
        self.assertIn("Тиснение у подрядчика Б", self.ai.call_args.args[0])
        self.cascade.assert_not_called()

    def test_invalid_ai_route_fails_without_catalog_fallback(self):
        self.ai.return_value = ({"route": {"processes": []}}, {})
        with self.assertRaises(TenderAIError):
            self.build()
        self.cascade.assert_not_called()

    def test_a_supply_role_step_sent_as_kind_is_accepted_as_catalog(self):
        """The model sometimes sends the process ROLE ("supply") where it
        should send the step KIND ("catalog") — a real, reproducible mix-up
        seen live (2026-09-29), not a hypothetical."""
        self.answer["route"]["processes"][0]["kind"] = "supply"
        result = self.build()
        self.assertEqual(result["route"]["processes"][0]["kind"], "catalog")

    def test_ui_offers_explicit_search_and_step_feedback(self):
        response = self.client.get(reverse("tender_home"))
        self.assertContains(response, "data-start-catalog")
        self.assertContains(response, "Подобрать товар")
        self.assertContains(response, "data-feedback-step")
        self.assertNotContains(response, "маршрут зафиксирован")

    def confirm(self, result):
        session = ProductionTrainingSession.objects.create(created_by=self.user, position_name=self.line["name"], requirements=self.line["requirements"], current_hypothesis=result)
        response = self.client.post(reverse("tender_confirm_production_type"), {"payload": json.dumps({"session_id": session.pk, "line": self.line})})
        self.assertEqual(response.status_code, 200, response.content)
        session.refresh_from_db()
        return session

    def test_replacing_confirmed_route_keeps_old_version_and_retires_its_lessons(self):
        original = self.confirm(self.build(feedback="Пакет закупаем"))
        old_example = original.confirmed_example
        self.answer["route"]["processes"] = [{"process_id": str(self.own_process.pk), "kind": "production", "details": []}]
        replacement = self.confirm(self.build(feedback="Пакет изготавливаем сами"))
        old_example.refresh_from_db()
        self.assertFalse(old_example.is_active)
        self.assertEqual(old_example.superseded_by_id, replacement.confirmed_example_id)
        self.assertFalse(Lesson.objects.get(session=original).is_active)
        self.assertTrue(Lesson.objects.get(session=replacement).is_active)
        self.assertEqual(old_example.routes[0]["processes"][0]["kind"], "catalog")

    def test_unrelated_positions_do_not_receive_route_lessons(self):
        session = self.confirm(self.build(feedback="Пакет закупаем у подрядчика А"))
        self.line["name"] = "Металлическая медаль"
        self.build()
        self.assertNotIn("Пакет закупаем у подрядчика А", self.ai.call_args.args[0])
        self.assertNotIn(f'"id": {session.confirmed_example_id}, "name": "Пакет"', self.ai.call_args.args[0])

    def test_disabling_lesson_disables_its_route_but_preserves_history(self):
        session = self.confirm(self.build(feedback="Пакет закупаем у подрядчика А"))
        lesson = Lesson.objects.get(session=session)
        response = self.client.post(reverse("tender_drop_route_knowledge"), {"payload": json.dumps({"lesson_id": lesson.pk})})
        self.assertEqual(response.status_code, 200)
        lesson.refresh_from_db()
        session.confirmed_example.refresh_from_db()
        self.assertFalse(lesson.is_active)
        self.assertFalse(session.confirmed_example.is_active)
        self.build()
        self.assertNotIn("Пакет закупаем у подрядчика А", self.ai.call_args.args[0])

    def test_legacy_frozen_example_is_not_used_as_route_knowledge(self):
        from .models import ProductionType
        ProductionTrainingExample.objects.create(
            production_type=ProductionType.objects.first(), position_name="Пакет", created_by=self.user,
            routes=[{"name": "Старая заглушка", "steps": ["Закупка готового изделия", "Нанесение"]}],
        )
        self.build()
        self.assertNotIn("Старая заглушка", self.ai.call_args.args[0])

    def test_explicit_start_endpoint_needs_no_feedback_and_passes_step(self):
        current = self.build()
        session = ProductionTrainingSession.objects.create(created_by=self.user, position_name="Пакет", current_hypothesis=current)
        with patch("tenders.views._submit_assistant_job") as submit, patch("tenders.views.build_training_hypothesis", return_value=current) as build:
            response = self.client.post(reverse("tender_revise_production_hypothesis"), {"payload": json.dumps({"session_id": session.pk, "line": self.line, "scope": "catalog", "step_id": "purchase"})})
            self.assertEqual(response.status_code, 202)
            submit.call_args.args[1](session)
            self.assertEqual(build.call_args.kwargs["step_id"], "purchase")
            self.assertEqual(build.call_args.kwargs["recompute"], "catalog")

    def test_requirements_toggle_before_search_does_not_run_any_job(self):
        current = self.build()
        session = ProductionTrainingSession.objects.create(created_by=self.user, position_name="Пакет", current_hypothesis=current)
        with patch("tenders.views._submit_assistant_job") as submit:
            response = self.client.post(reverse("tender_revise_production_hypothesis"), {"payload": json.dumps({"session_id": session.pk, "line": self.line, "scope": "requirements"})})
            self.assertEqual(response.status_code, 200)
            submit.assert_not_called()

    def test_catalog_results_survive_correction_of_another_stage(self):
        from .cascade import CascadeResult
        current = self.build()
        self.cascade.return_value = CascadeResult(item="пакет", queries=[], tz=[], candidates=[{"id": "x", "name": "Пакет", "price": "10", "fit": "exact"}], catalog_intent={}, requirement_selection=[])
        current = self.build(current=current, recompute="catalog", step_id="purchase")
        self.answer["route"]["processes"][1]["details"] = ["У подрядчика Б"]
        result = self.build(current=current, feedback="Тиснение у подрядчика Б", step_id="print")
        self.assertEqual(result["catalog_steps"]["purchase"]["catalog_selection"]["id"], "x")
        self.assertEqual(result["totals"]["cost_total"], "1000.00")
        self.assertEqual(self.cascade.call_count, 1)

    def test_removed_purchase_does_not_keep_its_price(self):
        from .cascade import CascadeResult
        current = self.build()
        self.cascade.return_value = CascadeResult(item="пакет", queries=[], tz=[], candidates=[{"id": "x", "name": "Пакет", "price": "10", "fit": "exact"}], catalog_intent={}, requirement_selection=[])
        current = self.build(current=current, recompute="catalog", step_id="purchase")
        self.answer["route"]["processes"] = [{"process_id": str(self.own_process.pk), "kind": "production", "details": []}]
        result = self.build(current=current, feedback="Делаем сами")
        self.assertEqual(result["costs"], [])
        self.assertEqual(result["totals"], {})
        self.assertNotIn("catalog_selection", result)

    def test_two_catalog_steps_keep_separate_products_and_costs(self):
        from .cascade import CascadeResult
        self.answer["route"]["processes"].append({"id": "box", "process_id": str(self.box_process.pk), "kind": "catalog", "catalog_item": "коробка", "details": []})
        current = self.build()
        self.cascade.return_value = CascadeResult(item="пакет", queries=[], tz=[], candidates=[{"id": "p", "name": "Пакет", "price": "10", "fit": "exact"}], catalog_intent={}, requirement_selection=[])
        current = self.build(current=current, recompute="catalog", step_id="purchase")
        self.cascade.return_value = CascadeResult(item="коробка", queries=[], tz=[], candidates=[{"id": "b", "name": "Коробка", "price": "5", "fit": "exact"}], catalog_intent={}, requirement_selection=[])
        result = self.build(current=current, recompute="catalog", step_id="box")
        self.assertEqual(result["catalog_steps"]["purchase"]["catalog_selection"]["id"], "p")
        self.assertEqual(result["catalog_steps"]["box"]["catalog_selection"]["id"], "b")
        self.assertEqual(result["totals"]["cost_total"], "1500.00")
        self.assertEqual(len(result["costs"]), 2)

    def test_answers_are_kept_only_in_the_current_route_dialogue(self):
        current = self.build()
        current["question_answers"] = {"format": "А4"}
        self.build(current=current)
        self.assertIn('"format": "А4"', self.ai.call_args.args[0])

    def test_proposed_process_is_created_only_when_route_is_confirmed(self):
        self.answer["route"] = {"reason": "Нужен отдельный этап", "processes": [{
            "id": "laser", "proposed_process": {"name": "Лазерная резка акрила", "role": "production", "description": "Когда нужна резка акрила"},
            "kind": "production", "details": ["После печати"],
        }]}
        result = self.build(feedback="Добавь лазерную резку акрила")
        self.assertFalse(ProcessDefinition.objects.filter(name="Лазерная резка акрила").exists())
        session = self.confirm(result)
        process = ProcessDefinition.objects.get(name="Лазерная резка акрила")
        self.assertTrue(process.is_active)
        self.assertEqual(session.confirmed_example.routes[0]["processes"][0]["process_id"], str(process.pk))

    def test_confirm_proposed_stage_creates_it_immediately_not_only_on_route_confirm(self):
        self.answer["route"] = {"reason": "Нужен отдельный этап", "processes": [{
            "id": "laser", "proposed_process": {"name": "Лазерная резка акрила", "role": "production", "description": "Когда нужна резка акрила"},
            "kind": "production", "details": [],
        }]}
        result = self.build(feedback="Добавь лазерную резку акрила")
        session = ProductionTrainingSession.objects.create(created_by=self.user, position_name="Пакет", requirements=self.line["requirements"], current_hypothesis=result)
        response = self.client.post(reverse("tender_confirm_proposed_stage"), {"payload": json.dumps({"session_id": session.pk, "step_id": "laser"})})
        self.assertEqual(response.status_code, 200, response.content)
        process = ProcessDefinition.objects.get(name="Лазерная резка акрила")
        self.assertTrue(process.performs_production)
        session.refresh_from_db()
        step = session.current_hypothesis["route"]["processes"][0]
        self.assertEqual(step["process_id"], str(process.pk))
        self.assertNotIn("proposed_process", step)
        self.assertFalse(session.is_confirmed)

    def test_confirm_proposed_stage_rejects_unknown_step(self):
        result = self.build()
        session = ProductionTrainingSession.objects.create(created_by=self.user, position_name="Пакет", current_hypothesis=result)
        response = self.client.post(reverse("tender_confirm_proposed_stage"), {"payload": json.dumps({"session_id": session.pk, "step_id": "nope"})})
        self.assertEqual(response.status_code, 400)

    def test_current_order_feedback_is_not_saved_as_a_lesson(self):
        result = self.build(current=self.build(), feedback="Только для этого заказа", learn_for_similar=False)
        session = self.confirm(result)
        self.assertFalse(Lesson.objects.filter(session=session, admin_text="Только для этого заказа").exists())
        self.assertFalse(session.confirmed_example.is_active)

    def test_unknown_feedback_action_is_not_exposed_as_backend_action(self):
        self.answer["feedback_actions"] = [
            {"type": "remove_stage", "stage_id": "purchase", "summary": "Убрана закупка"},
            {"type": "arbitrary_sql", "summary": "Не должно попасть в план"},
        ]
        result = self.build()
        self.assertEqual(result["feedback_actions"], [{"type": "remove_stage", "stage_id": "purchase", "summary": "Убрана закупка"}])
