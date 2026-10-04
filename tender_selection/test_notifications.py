from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from . import services
from .models import Tender
from .notification import parse_notification


def _joint_purchase_payload():
    def requirement(name, guarantee):
        return {
            "customer": {"fullName": name},
            "applicationGuarantee": {"amount": guarantee},
            "contractConditionsInfo": {},
        }

    return {
        "doc_type": "epNotificationEF2020",
        "source": {
            "notificationInfo": {
                "customerRequirementsInfo": {
                    "customerRequirementInfo": [requirement("Первый заказчик", "1000"), requirement("Второй заказчик", "2000")],
                },
            },
        },
    }


class JointPurchaseNotificationTests(SimpleTestCase):
    def test_joint_purchase_uses_first_customer_requirement(self):
        card = parse_notification(_joint_purchase_payload())

        self.assertEqual(card["customer"]["name"], "Первый заказчик")
        self.assertEqual(card["money"]["app_guarantee_amount"], "1000")


class RetryPendingNotificationsTests(TestCase):
    def _tender(self, number, *, checked_ago=None, raw=None, closes_in=timedelta(days=5)):
        now = timezone.now()
        return Tender.objects.create(
            purchase_number=number,
            notification_raw=raw or {},
            notification_checked_at=now - checked_ago if checked_ago is not None else None,
            collecting_finished_at=now + closes_in,
            last_pulled_at=now,
        )

    def _retried_numbers(self):
        with patch.object(services, "notification_for", return_value=None) as notification_for:
            services.retry_pending_notifications()
        return {call.args[0].purchase_number for call in notification_for.call_args_list}

    def test_never_checked_tender_is_fetched(self):
        self._tender("never")
        self.assertEqual(self._retried_numbers(), {"never"})

    def test_failed_fetch_is_retried_after_cooldown(self):
        self._tender("failed-long-ago", checked_ago=services.NOTIFICATION_RETRY_COOLDOWN + timedelta(minutes=1))
        self.assertEqual(self._retried_numbers(), {"failed-long-ago"})

    def test_recent_failure_waits_for_cooldown(self):
        self._tender("failed-just-now", checked_ago=timedelta(minutes=5))
        self.assertEqual(self._retried_numbers(), set())

    def test_loaded_or_closed_tenders_are_not_retried(self):
        long_ago = services.NOTIFICATION_RETRY_COOLDOWN + timedelta(hours=1)
        self._tender("loaded", checked_ago=long_ago, raw={"source": {}})
        self._tender("closed", checked_ago=long_ago, closes_in=-timedelta(days=1))
        self.assertEqual(self._retried_numbers(), set())

    def test_temporary_error_is_recorded_separately_from_missing_notification(self):
        tender = self._tender("temporary-error")
        with patch.object(services.gosplan, "fetch_notification", side_effect=services.gosplan.GosplanError("429")):
            self.assertIsNone(services.notification_for(tender))

        tender.refresh_from_db()
        self.assertEqual(tender.notification_error, "429")
