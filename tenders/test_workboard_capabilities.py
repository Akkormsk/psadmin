from django.contrib.auth import get_user_model
from django.test import TestCase

from .assistant_coverage import audit_user_operation_coverage
from .assistant_tools import AssistantToolError, execute_tool
from .models import WorkBoard
from .workboards import create_board, create_task_card


class WorkBoardCapabilityTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("capability-owner", password="password")
        self.other = get_user_model().objects.create_user("capability-other", password="password")

    def test_create_board_uses_registered_confirmed_action(self):
        result = execute_tool("workboard.create", self.owner, {"name": "Новая доска"}, confirmed=True)

        self.assertEqual(WorkBoard.objects.get(pk=result["board_id"]).created_by, self.owner)

    def test_card_action_rejects_another_users_board(self):
        board = create_board(self.owner, "Личная доска")
        card = create_task_card(board, name="Закрытая задача")
        done = board.columns.get(kind="done")

        with self.assertRaisesMessage(AssistantToolError, "Нет доступа"):
            execute_tool("workboard.card_move", self.other, {
                "card_id": card.pk, "column_id": done.pk,
            }, confirmed=True)

    def test_every_discovered_workboard_operation_is_covered(self):
        missing = [
            result.operation.identifier
            for result in audit_user_operation_coverage(self.owner)
            if result.operation.executor.startswith("tenders.workboards.") and not result.covered
        ]

        self.assertEqual(missing, [])
