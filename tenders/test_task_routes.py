from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .models import WorkBoardCard, WorkCheckpoint, WorkCheckpointEvent, WorkBoardColumn
from .workboards import create_board, create_task_from_plan, edit_task_work, move_card, task_progress


class TaskRouteTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("route-owner")
        self.other = get_user_model().objects.create_user("route-other")
        self.board = create_board(self.owner, "Разработка")
        self.payload = {"name": "Надёжность чтения", "items": [{"name": "Чтение документов", "steps": [
            {"name": "Извлечение", "checks": ["Таблицы извлечены", "Источники сохранены"]},
            {"name": "Приёмка", "checks": ["Повтор проверен"]}]}]}
        self.card = create_task_from_plan(self.board, self.owner, self.payload)
        self.checks = list(WorkCheckpoint.objects.order_by("step__position", "position"))
        self.done = self.board.columns.get(kind=WorkBoardColumn.Kind.DONE)

    def toggle(self, check, value):
        edit_task_work(self.card, self.owner, "toggle_check", {"checkpoint_id": check.pk, "completed": value})

    def test_sequential_completion_audit_and_done(self):
        with self.assertRaises(ValidationError):
            self.toggle(self.checks[2], True)
        with self.assertRaises(ValidationError):
            move_card(self.card, self.done)
        for check in self.checks:
            self.toggle(check, True)
        self.toggle(self.checks[2], True)
        self.assertEqual(WorkCheckpointEvent.objects.count(), 3)
        self.assertEqual(task_progress(self.card.task)["percent"], 100)
        move_card(self.card, self.done)
        with self.assertRaises(ValidationError):
            self.toggle(self.checks[0], False)
        self.toggle(self.checks[2], False)
        self.card.refresh_from_db()
        self.assertIsNone(self.card.completed_at)
        self.assertEqual(self.card.column.kind, WorkBoardColumn.Kind.QUEUE)
        self.assertEqual(WorkCheckpointEvent.objects.count(), 4)

    def test_detail_and_cross_card_permission(self):
        url = reverse("workboard_task_detail", args=[self.card.pk])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(url), "Чтение документов")
        self.assertContains(self.client.get(url), "Источники сохранены")
        other = create_task_from_plan(self.board, self.owner, {"name": "Другая задача"})
        response = self.client.post(reverse("workboard_task_edit", args=[other.pk]),
                                    {"action": "toggle_check", "checkpoint_id": self.checks[0].pk, "completed": "1"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(WorkCheckpoint.objects.get(pk=self.checks[0].pk).completed_at)

    def test_import_rolls_back_and_returns_actual_card(self):
        self.client.force_login(self.owner)
        url = reverse("workboard_task_import", args=[self.board.pk])
        before = WorkBoardCard.objects.count()
        response = self.client.post(url, {"name": "Неполная", "items": [{"name": "Без проверки", "steps": [{"name": "Этап"}]}]}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(WorkBoardCard.objects.count(), before)
        response = self.client.post(url, self.payload, content_type="application/json")
        self.assertEqual(response.status_code, 201)
        card = WorkBoardCard.objects.get(pk=response.json()["card_id"])
        self.assertEqual(task_progress(card.task)["total"], 1)
        self.assertEqual(card.pk, self.card.pk)
        self.assertEqual(WorkBoardCard.objects.count(), before)

    def test_manual_stage_cannot_be_saved_for_commercial_item(self):
        from tender_selection.models import Tender
        from .models import TenderCommercialItem, ComponentRoutePlan, ComponentOperationStep
        tender = Tender.objects.create(purchase_number="777", title="Печать")
        item = TenderCommercialItem.objects.create(tender=tender, source_key="source", display_name="Печать")
        plan = ComponentRoutePlan.objects.create(commercial_item=item, scope=ComponentRoutePlan.Scope.SHARED)
        with self.assertRaises(ValidationError):
            ComponentOperationStep.objects.create(route_plan=plan, manual_name="Ручной этап", position=0)

    def test_delete_imported_task_with_route_and_history(self):
        from .models import Task, TenderCommercialItem, ComponentRoutePlan, ComponentOperationStep
        self.toggle(self.checks[0], True)
        other = create_task_from_plan(self.board, self.owner, {
            "name": "Другая задача",
            "items": [{"name": "Проверка", "steps": [{"name": "Этап", "checks": ["Готово"]}]}],
        })
        other_check = WorkCheckpoint.objects.get(step__route_plan__commercial_item__task_id=other.task_id)
        edit_task_work(other, self.owner, "toggle_check", {"checkpoint_id": other_check.pk, "completed": True})
        task_id = self.card.task_id
        item_ids = list(TenderCommercialItem.objects.filter(task_id=task_id).values_list("pk", flat=True))
        plan_ids = list(ComponentRoutePlan.objects.filter(commercial_item_id__in=item_ids).values_list("pk", flat=True))
        step_ids = list(ComponentOperationStep.objects.filter(route_plan_id__in=plan_ids).values_list("pk", flat=True))
        check_ids = [check.pk for check in self.checks]
        self.client.force_login(self.owner)
        response = self.client.post(reverse("workboard_card_delete", args=[self.card.pk]))
        self.assertRedirects(response, f"{reverse('workboard_list')}?board={self.board.pk}")
        self.assertFalse(WorkBoardCard.objects.filter(pk=self.card.pk).exists())
        self.assertFalse(Task.objects.filter(pk=task_id).exists())
        self.assertFalse(TenderCommercialItem.objects.filter(pk__in=item_ids).exists())
        self.assertFalse(ComponentRoutePlan.objects.filter(pk__in=plan_ids).exists())
        self.assertFalse(ComponentOperationStep.objects.filter(pk__in=step_ids).exists())
        self.assertFalse(WorkCheckpoint.objects.filter(pk__in=check_ids).exists())
        self.assertFalse(WorkCheckpointEvent.objects.filter(checkpoint_id__in=check_ids).exists())
        self.assertTrue(WorkBoardCard.objects.filter(pk=other.pk).exists())
        self.assertTrue(WorkCheckpointEvent.objects.filter(checkpoint_id=other_check.pk).exists())
