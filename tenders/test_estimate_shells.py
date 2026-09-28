from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tender_selection.models import Tender

from .models import OrderEstimate, TenderEstimate


class EstimateShellTests(TestCase):
    """Одно и то же ядро (таблица + калькулятор), три разные обёртки:
    pipeline (вход из карточки тендера — минимальный вид), worklist (вход
    из списка «Расчёты» — с блоком рисков), order (самостоятельный расчёт)."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="manager", password="test-pass")
        self.client.force_login(self.user)

    def _tender(self, **fields):
        fields.setdefault("purchase_number", "1")
        return Tender.objects.create(law="fz44", object_info="Тендер", **fields)

    def test_pipeline_shell_hides_rename_new_calc_and_risk_block(self):
        tender = self._tender(risk_assessment={"delivery_mode": "разовая поставка"})
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")

        response = self.client.get(reverse("tender_pipeline_estimate", args=[estimate.pk]))

        self.assertNotContains(response, "Номер тендера<input")
        self.assertNotContains(response, 'id="new-tender"')
        self.assertNotContains(response, "Важное для расчёта")
        self.assertContains(response, "Применить и вернуться к тендеру")

    def test_worklist_shell_shows_risk_block_and_hides_rename(self):
        tender = self._tender(risk_assessment={
            "delivery_mode": "поставка по заявкам",
            "sample_requirements": "образцы не требуются",
            "national_regime": "признаков не найдено",
            "execution_deadline": {"date": "01.01.2027"},
            "risk_facts": {"batch_days": 10},
        })
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")

        response = self.client.get(reverse("tender_worklist_estimate", args=[estimate.pk]))

        self.assertNotContains(response, "Номер тендера<input")
        self.assertNotContains(response, "Исходный тендер")
        self.assertNotContains(response, "Применить и вернуться к тендеру")
        self.assertContains(response, "Важное для расчёта")
        self.assertContains(response, "поставка по заявкам")
        self.assertContains(response, "10 дн.")

    def test_worklist_shell_degrades_gracefully_without_risk_assessment(self):
        tender = self._tender()
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")

        response = self.client.get(reverse("tender_worklist_estimate", args=[estimate.pk]))

        self.assertContains(response, "Важное для расчёта")
        self.assertContains(response, "ещё не проводилась")

    def test_worklist_shell_keeps_new_calc_button(self):
        tender = self._tender()
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")

        response = self.client.get(reverse("tender_worklist_estimate", args=[estimate.pk]))

        self.assertContains(response, 'id="new-tender"')

    def test_neither_shell_offers_duplicate_or_delete_on_the_page(self):
        tender = self._tender()
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")

        for name in ("tender_pipeline_estimate", "tender_worklist_estimate"):
            response = self.client.get(reverse(name, args=[estimate.pk]))
            self.assertNotContains(response, "Копировать просчёт")
            self.assertNotContains(response, "Удалить просчёт")

    def test_order_shell_keeps_rename_and_duplicate_delete(self):
        order = OrderEstimate.objects.create(owner=self.user, order_number="1", name="Самостоятельный")

        response = self.client.get(reverse("tender_estimate", args=[order.pk]))

        self.assertContains(response, 'name="tender_number"')
        self.assertContains(response, "Копировать просчёт")
        self.assertContains(response, "Удалить просчёт")
        self.assertNotContains(response, "Важное для расчёта")

    def test_worklist_list_links_tender_rows_to_the_worklist_shell(self):
        tender = self._tender()
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")

        response = self.client.get(reverse("tender_home"), {"kind": "tender", "worklist": ""})

        self.assertContains(response, reverse("tender_worklist_estimate", args=[estimate.pk]))

    def test_header_shows_title_customer_and_number(self):
        """Название задаёт тендер, «имя» расчёта по договорённости хранит
        заказчика (см. push_to_estimate) — обе шапки-обёртки показывают все три."""
        tender = self._tender(title="Печать открыток")
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="0139", name="ДКС Кузбасса")

        for shell_name in ("tender_pipeline_estimate", "tender_worklist_estimate"):
            response = self.client.get(reverse(shell_name, args=[estimate.pk]))
            self.assertContains(response, "Печать открыток")
            self.assertContains(response, "ДКС Кузбасса")
            self.assertContains(response, "0139")

    def test_risk_block_shows_submission_deadline_even_without_assessment(self):
        deadline = timezone.now() + timedelta(days=5)
        tender = self._tender(collecting_finished_at=deadline)
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")

        response = self.client.get(reverse("tender_worklist_estimate", args=[estimate.pk]))

        self.assertContains(response, "Подача заявки")
        self.assertContains(response, deadline.strftime("%d.%m.%Y"))

    def test_worklist_shell_still_shows_the_saved_list_below(self):
        """«Не плодить ссылки» — тот же список видим и когда открыт конкретный
        расчёт, просто форма сверху уже заполнена."""
        tender = self._tender()
        mine = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")
        other_tender = self._tender(purchase_number="2")
        other = TenderEstimate.objects.create(owner=self.user, tender=other_tender, tender_number="2", name="Другое")

        response = self.client.get(reverse("tender_worklist_estimate", args=[mine.pk]))

        self.assertContains(response, "Актуальные расчёты")
        self.assertContains(response, other.name)

    def test_pipeline_shell_never_shows_the_saved_list(self):
        tender = self._tender()
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="1", name="Моё")

        response = self.client.get(reverse("tender_pipeline_estimate", args=[estimate.pk]))

        self.assertNotContains(response, "Актуальные расчёты")
