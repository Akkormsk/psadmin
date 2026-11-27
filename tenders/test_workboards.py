from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from tender_selection.models import Tender

from .models import WorkBoardCard, WorkBoardColumn
from .workboards import add_column, create_board, create_task_card, create_tender_card, move_card


class WorkBoardServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("board-owner", password="password")
        self.board = create_board(self.user, "Разработка")

    def test_new_board_has_required_edge_columns(self):
        columns = list(self.board.columns.values_list("name", "kind", "position"))

        self.assertEqual(columns, [
            ("В очереди", WorkBoardColumn.Kind.QUEUE, 0),
            ("Готово", WorkBoardColumn.Kind.DONE, 1),
        ])

    def test_custom_column_is_inserted_before_done(self):
        add_column(self.board, "В работе")

        self.assertEqual(
            list(self.board.columns.values_list("name", "position")),
            [("В очереди", 0), ("В работе", 1), ("Готово", 2)],
        )

    def test_task_starts_in_queue_and_done_marks_it_completed(self):
        card = create_task_card(self.board, name="Проверить макет")
        done = self.board.columns.get(kind=WorkBoardColumn.Kind.DONE)

        self.assertEqual(card.column.kind, WorkBoardColumn.Kind.QUEUE)
        move_card(card, done)
        card.refresh_from_db()
        self.assertIsNotNone(card.completed_at)

        queue = self.board.columns.get(kind=WorkBoardColumn.Kind.QUEUE)
        move_card(card, queue)
        card.refresh_from_db()
        self.assertIsNone(card.completed_at)

    def test_card_cannot_move_to_another_board_column(self):
        card = create_task_card(self.board, name="Сверить цены")
        other_board = create_board(self.user, "Закупки")
        other_queue = other_board.columns.get(kind=WorkBoardColumn.Kind.QUEUE)

        with self.assertRaises(ValidationError):
            move_card(card, other_queue)

    def test_only_won_tender_can_be_added(self):
        tender = Tender.objects.create(purchase_number="100", title="Печать календарей")

        with self.assertRaises(ValidationError):
            create_tender_card(self.board, tender)

        tender.outcome_status = Tender.OUTCOME_WON
        tender.save(update_fields=["outcome_status"])
        card = create_tender_card(self.board, tender)

        self.assertEqual(card.tender, tender)
        self.assertEqual(card.column.kind, WorkBoardColumn.Kind.QUEUE)


class WorkBoardViewTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("view-owner", password="password")
        self.other = get_user_model().objects.create_user("view-other", password="password")
        self.board = create_board(self.owner, "Моя работа")

    def test_owner_can_create_and_move_task(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("workboard_task_create", args=[self.board.pk]),
            {"name": "Подготовить карточку", "description": "До пятницы"},
        )
        card = WorkBoardCard.objects.get(task__name="Подготовить карточку")
        done = self.board.columns.get(kind=WorkBoardColumn.Kind.DONE)

        self.assertRedirects(response, f"{reverse('workboard_list')}?board={self.board.pk}")
        response = self.client.post(
            reverse("workboard_card_move", args=[card.pk]),
            {"column_id": done.pk},
        )
        card.refresh_from_db()

        self.assertRedirects(response, f"{reverse('workboard_list')}?board={self.board.pk}")
        self.assertIsNotNone(card.completed_at)

    def test_another_user_cannot_open_or_change_board(self):
        self.client.force_login(self.other)

        self.assertEqual(
            self.client.get(f"{reverse('workboard_list')}?board={self.board.pk}").status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("workboard_task_create", args=[self.board.pk]),
                {"name": "Чужая задача"},
            ).status_code,
            403,
        )

    def test_board_page_shows_task_and_required_columns(self):
        create_task_card(self.board, name="Залить на тест")
        self.client.force_login(self.owner)

        response = self.client.get(f"{reverse('workboard_list')}?board={self.board.pk}")

        self.assertContains(response, "Залить на тест")
        self.assertContains(response, "В очереди")
        self.assertContains(response, "Готово")
        self.assertContains(response, '<h1>Тендеры</h1>', html=True)
        self.assertContains(response, 'class="panel ts-sheet workboards-page"')
        self.assertContains(response, 'class="kb-card"')
        self.assertIsNone(response.context["nav_counts"])

    def test_admin_board_uses_existing_trading_navigation_counts(self):
        admin = get_user_model().objects.create_superuser("board-nav-admin", "nav@example.test", "password")
        self.client.force_login(admin)
        from tender_selection.models import FilterSettings
        from tender_selection.views import _incoming_count, _board_count

        settings = FilterSettings.load()
        response = self.client.get(reverse("workboard_list"))
        self.assertEqual(response.context["nav_counts"], {
            "incoming": _incoming_count(settings), "board": _board_count(settings),
        })

    def test_superuser_can_add_won_tender(self):
        admin = get_user_model().objects.create_superuser("board-admin", "admin@example.test", "password")
        tender = Tender.objects.create(
            purchase_number="200",
            title="Поставка блокнотов",
            outcome_status=Tender.OUTCOME_WON,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("workboard_tender_add", args=[tender.pk]),
            {"board_id": self.board.pk},
        )

        self.assertRedirects(response, reverse("tender_selection:detail", args=[tender.pk]), fetch_redirect_response=False)
        self.assertTrue(WorkBoardCard.objects.filter(board=self.board, tender=tender).exists())
        self.client.post(reverse("workboard_tender_add", args=[tender.pk]), {"board_id": self.board.pk})
        self.assertEqual(WorkBoardCard.objects.filter(tender=tender).count(), 1)
        page = self.client.get(reverse("workboard_list"))
        self.assertContains(page, "workboards.css")
        self.assertNotContains(page, "Выберите тендер")
