from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from . import gosplan, services
from .models import FilterSettings, Tender


class RetryPendingDeadlinesTests(TestCase):
    """Тендер, выпавший из окна публикации (window_days), больше никогда не
    попадает в обычный сбор — продление срока заказчиком иначе останется
    незамеченным навсегда, и тендер тихо пропадёт из «Входящих»/архива по
    старой, уже неверной дате. Один контрольный запрос по номеру перед этим."""

    def setUp(self):
        FilterSettings.load()  # incoming_ttl_days=7 по умолчанию

    def _tender(self, number, *, closed_ago):
        now = timezone.now()
        return Tender.objects.create(
            purchase_number=number, law="fz44", object_info="Тест",
            collecting_finished_at=now - closed_ago, last_pulled_at=now,
        )

    def test_extended_deadline_is_picked_up(self):
        tender = self._tender("extended", closed_ago=timedelta(days=1))
        new_deadline = timezone.now() + timedelta(days=2)

        with patch.object(gosplan, "fetch_purchase", return_value={
            "object_info": "Тест", "collecting_finished_at": new_deadline.isoformat(),
        }):
            attempted, succeeded = services.retry_pending_deadlines()

        self.assertEqual((attempted, succeeded), (1, 1))
        tender.refresh_from_db()
        self.assertEqual(tender.collecting_finished_at, new_deadline)

    def test_genuinely_expired_tender_is_left_alone(self):
        tender = self._tender("really-over", closed_ago=timedelta(days=1))
        old_deadline = tender.collecting_finished_at

        with patch.object(gosplan, "fetch_purchase", return_value={
            "object_info": "Тест", "collecting_finished_at": old_deadline.isoformat(),
        }):
            attempted, succeeded = services.retry_pending_deadlines()

        self.assertEqual((attempted, succeeded), (1, 0))
        tender.refresh_from_db()
        self.assertEqual(tender.collecting_finished_at, old_deadline)

    def test_tender_still_open_is_not_rechecked(self):
        self._tender("still-open", closed_ago=timedelta(days=-5))

        with patch.object(gosplan, "fetch_purchase") as fetch_purchase:
            services.retry_pending_deadlines()

        fetch_purchase.assert_not_called()

    def test_tender_past_the_cleanup_ttl_is_not_rechecked(self):
        """purge_stale удалит его в этом же цикле — незачем тратить запрос."""
        self._tender("long-gone", closed_ago=timedelta(days=30))

        with patch.object(gosplan, "fetch_purchase") as fetch_purchase:
            services.retry_pending_deadlines()

        fetch_purchase.assert_not_called()

    def test_gosplan_error_is_swallowed(self):
        self._tender("flaky", closed_ago=timedelta(days=1))

        with patch.object(gosplan, "fetch_purchase", side_effect=gosplan.GosplanError("boom")):
            attempted, succeeded = services.retry_pending_deadlines()

        self.assertEqual((attempted, succeeded), (1, 0))
