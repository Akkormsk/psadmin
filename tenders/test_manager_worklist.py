from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tender_selection.models import Tender

from .models import OrderEstimate, TenderEstimate


class ManagerWorklistTests(TestCase):
    """Менеджер видит свои TenderEstimate и OrderEstimate в одном списке
    «Расчёты», но не может менять статус TenderEstimate вручную — тот
    управляется только из пайплайна тендера."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="manager", password="test-pass")
        self.other = get_user_model().objects.create_user(username="other", password="test-pass")
        self.client.force_login(self.user)

    def test_tender_estimate_assigned_to_manager_appears_in_the_list(self):
        tender = Tender.objects.create(law="fz44", purchase_number="1", object_info="Тендер")
        mine = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")
        TenderEstimate.objects.create(owner=self.other, tender_number="2", name="Чужое")

        response = self.client.get(reverse("tender_home"))

        self.assertContains(response, mine.name)
        self.assertNotContains(response, "Чужое")

    def test_tender_estimate_row_has_no_editable_status_selector(self):
        tender = Tender.objects.create(law="fz44", purchase_number="1", object_info="Тендер")
        TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")

        response = self.client.get(reverse("tender_home"))

        self.assertNotContains(response, "order_estimate_status")

    def test_order_estimate_row_keeps_its_editable_status_selector(self):
        OrderEstimate.objects.create(owner=self.user, order_number="1", name="Самостоятельный")

        response = self.client.get(reverse("tender_home"), {"kind": "order"})

        self.assertContains(response, "data-estimate-status-form")

    def test_draft_tender_estimate_shows_as_draft_or_ready_by_completeness(self):
        tender = Tender.objects.create(law="fz44", purchase_number="1", object_info="Тендер")
        empty = TenderEstimate.objects.create(
            owner=self.user, tender=tender, tender_number="1", name="Пустой",
            summary_snapshot={"is_incomplete": True},
        )
        filled = TenderEstimate.objects.create(
            owner=self.user, tender=tender, tender_number="2", name="Заполненный",
            summary_snapshot={"is_incomplete": False},
        )

        self.assertEqual(empty.display_status(), "Черновик")
        self.assertEqual(filled.display_status(), "Готово")

    def test_non_draft_tender_estimate_shows_its_real_status(self):
        tender = Tender.objects.create(law="fz44", purchase_number="1", object_info="Тендер")
        estimate = TenderEstimate.objects.create(
            owner=self.user, tender=tender, tender_number="1", name="На торгах",
            status=TenderEstimate.PENDING,
        )

        self.assertEqual(estimate.display_status(), "На торгах")

    def test_worklist_tab_separates_active_from_ready(self):
        tender = Tender.objects.create(law="fz44", purchase_number="1", object_info="Тендер")
        draft = TenderEstimate.objects.create(
            owner=self.user, tender=tender, tender_number="1", name="Незаполненный",
            summary_snapshot={"is_incomplete": True},
        )
        ready = TenderEstimate.objects.create(
            owner=self.user, tender=tender, tender_number="2", name="Заполненный",
            summary_snapshot={"is_incomplete": False},
        )
        won = TenderEstimate.objects.create(
            owner=self.user, tender=tender, tender_number="3", name="Выигранный",
            status=TenderEstimate.WON, summary_snapshot={"is_incomplete": False},
        )

        active = self.client.get(reverse("tender_home"), {"worklist": "active"})
        self.assertContains(active, draft.name)
        self.assertNotContains(active, ready.name)
        self.assertNotContains(active, won.name)

        done = self.client.get(reverse("tender_home"), {"worklist": "ready"})
        self.assertNotContains(done, draft.name)
        self.assertContains(done, ready.name)
        self.assertContains(done, won.name)

    def test_kind_tab_separates_tenders_from_standalone_orders(self):
        tender = Tender.objects.create(law="fz44", purchase_number="1", object_info="Тендер")
        TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Тендерный")
        OrderEstimate.objects.create(owner=self.user, order_number="1", name="Самостоятельный")

        tenders_only = self.client.get(reverse("tender_home"), {"kind": "tender"})
        self.assertContains(tenders_only, "Тендерный")
        self.assertNotContains(tenders_only, "Самостоятельный")

        orders_only = self.client.get(reverse("tender_home"), {"kind": "order"})
        self.assertNotContains(orders_only, "Тендерный")
        self.assertContains(orders_only, "Самостоятельный")

    def test_page_opens_on_active_tenders_by_default(self):
        tender = Tender.objects.create(law="fz44", purchase_number="1", object_info="Тендер")
        active_tender = TenderEstimate.objects.create(
            owner=self.user, tender=tender, tender_number="1", name="В работе",
            summary_snapshot={"is_incomplete": True},
        )
        ready_tender = TenderEstimate.objects.create(
            owner=self.user, tender=tender, tender_number="2", name="Готовый тендер",
            summary_snapshot={"is_incomplete": False},
        )
        OrderEstimate.objects.create(owner=self.user, order_number="1", name="Самостоятельный")

        response = self.client.get(reverse("tender_home"))

        self.assertContains(response, active_tender.name)
        self.assertNotContains(response, ready_tender.name)
        self.assertNotContains(response, "Самостоятельный")

    def test_row_shows_an_unobtrusive_kind_label(self):
        tender = Tender.objects.create(law="fz44", purchase_number="1", object_info="Тендер")
        TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Тендерный")
        OrderEstimate.objects.create(owner=self.user, order_number="1", name="Самостоятельный")

        response = self.client.get(reverse("tender_home"), {"kind": "", "worklist": ""})

        self.assertContains(response, '<span class="saved-estimate__kind">Тендер</span>')
        self.assertContains(response, '<span class="saved-estimate__kind">Заказ</span>')
