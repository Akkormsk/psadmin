import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .cascade import Cascade
from .models import CascadeLabCase, CascadeLabRun, TenderEstimate, TenderLine


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
            "step_settings_json": json.dumps({"2": {"max_phrases": 8}}),
        })

        self.assertEqual(response.status_code, 202)
        self.assertEqual(CascadeLabRun.objects.get().settings["steps"]["2"]["max_phrases"], 8)
        submit.assert_called_once()

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

    def test_lab_step_settings_do_not_change_default_cascade(self):
        default = Cascade({"name": "Товар"})
        default.item, default.queries = "товар", ["товар", "изделие"]
        configured = Cascade({"name": "Товар"}, step_settings={"2": {"max_phrases": 1}})
        configured.item, configured.queries = "товар", ["товар", "изделие"]
        self.assertEqual(default.step_2_search_plan(), ["товар", "изделие"])
        self.assertEqual(configured.step_2_search_plan(), ["товар"])

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
