import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, SimpleTestCase

from . import discovery_feed, gosplan, services
from .models import Tender, DiscoveryCursor


class DiscoveryFeedTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"TENDER_DISCOVERY_FEED_DIR": self.directory.name,
                                                   "TENDER_DISCOVERY_ROLE": "collector"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.record = {"purchase_number": "1234567890123456789", "object_info": "Поставка бумаги", "max_price": "500000"}

    def test_import_is_idempotent_and_preserves_local_work(self):
        discovery_feed.publish(self.record, "fz44")
        with patch.dict(os.environ, {"TENDER_DISCOVERY_ROLE": "mirror"}), patch.object(gosplan, "_get") as network:
            first = services.run_pull()
            tender = Tender.objects.get()
            Tender.objects.filter(pk=tender.pk).update(status=Tender.PUSHED, review=Tender.INTERESTING,
                                                      bid_number="ours", notification_raw={"retained": True})
            second = services.run_pull()
        self.assertTrue(first.ok)
        self.assertEqual(first.created_count, 1)
        self.assertEqual(second.created_count, 0)
        self.assertEqual(second.updated_count, 0)
        network.assert_not_called()
        tender.refresh_from_db()
        self.assertEqual(tender.status, Tender.PUSHED)
        self.assertEqual(tender.bid_number, "ours")
        self.assertEqual(tender.notification_raw, {"retained": True})

    def test_changed_primary_data_does_not_reset_decisions(self):
        tender = Tender.objects.create(law="fz44", purchase_number=self.record["purchase_number"],
                                       raw=self.record, status=Tender.PUSHED, outcome_status=Tender.OUTCOME_WON)
        discovery_feed.publish({**self.record, "max_price": "600000"}, "fz44")
        self.assertTrue(discovery_feed.import_feed().ok)
        tender.refresh_from_db()
        self.assertEqual(tender.max_price, 600000)
        self.assertEqual(tender.status, Tender.PUSHED)
        self.assertEqual(tender.outcome_status, Tender.OUTCOME_WON)

    def test_malformed_feed_is_not_reported_as_complete(self):
        Path(self.directory.name, "bad.json").write_text(json.dumps({"law": "bad", "record": self.record}))
        self.assertFalse(discovery_feed.import_feed().ok)
        self.assertEqual(Tender.objects.count(), 0)

    def test_mirror_keyword_pass_does_not_call_gosplan(self):
        with patch.dict(os.environ, {"TENDER_DISCOVERY_ROLE": "mirror"}), patch.object(gosplan, "iter_purchases") as network:
            self.assertTrue(services.run_keyword_pull().ok)
        network.assert_not_called()

    def test_failed_page_never_advances_cursor_and_can_be_replayed(self):
        def partial(**kwargs):
            yield self.record
            raise gosplan.GosplanError("upstream failed")
        with patch.object(gosplan, "iter_purchases", side_effect=partial):
            run = services.run_pull(classifiers=["17"], laws=["fz44"])
        self.assertFalse(run.ok)
        self.assertEqual(DiscoveryCursor.objects.count(), 0)
        self.assertEqual(len(list(Path(self.directory.name).glob("*.json"))), 1)
        self.assertTrue(discovery_feed.import_feed().ok)
        self.assertEqual(Tender.objects.count(), 1)

    def test_initial_backfill_does_not_start_paid_profile_triage(self):
        with patch.object(gosplan, "iter_purchases", return_value=iter([self.record])), \
                patch("tender_selection.profile_triage.start_profile_triage_in_background") as ai:
            self.assertTrue(services.run_pull(classifiers=["17"], laws=["fz44"]).ok)
        ai.assert_not_called()

    def test_next_completed_scan_uses_update_cursor(self):
        with patch.object(gosplan, "iter_purchases", return_value=iter([])):
            self.assertTrue(services.run_pull(classifiers=["17"], laws=["fz44"]).ok)
        self.assertEqual(DiscoveryCursor.objects.count(), 1)
        with patch.object(gosplan, "iter_purchases", return_value=iter([])) as request:
            self.assertTrue(services.run_pull(classifiers=["17"], laws=["fz44"]).ok)
        self.assertIn("updated_after", request.call_args.kwargs["params"])
        self.assertEqual(request.call_args.kwargs["params"]["classifier"], ["17"])

    def test_eis_failure_serves_cached_notice_and_preserves_error(self):
        payload = {"source": {"commonInfo": {"purchaseNumber": self.record["purchase_number"]}}}
        tender = Tender.objects.create(purchase_number=self.record["purchase_number"], notification_raw=payload)
        with patch.object(gosplan, "fetch_notification", side_effect=gosplan.GosplanError("ЕИС временно заблокирован")):
            self.assertEqual(services.notification_for(tender, force=True), payload)
        tender.refresh_from_db()
        self.assertEqual(tender.notification_raw, payload)
        self.assertIn("заблокирован", tender.notification_error)


class DiscoveryOnlyTests(SimpleTestCase):
    def test_secondary_endpoints_cannot_use_gosplan_network(self):
        with patch.object(gosplan, "urlopen") as request:
            for path in ("/fz44/contracts", "/fz44/organizations", "/fz44/purchases/123", "/fz44/purchases/123/notification"):
                with self.assertRaises(gosplan.GosplanError):
                    gosplan._get(path, {})
        request.assert_not_called()

    def test_legacy_secondary_entrypoint_delegates_to_eis(self):
        with patch("tender_selection.eis_source.notification", return_value={"source": {"official": True}}) as direct, \
                patch.object(gosplan, "urlopen") as request:
            self.assertEqual(gosplan.fetch_notification("123"), {"source": {"official": True}})
        direct.assert_called_once_with("123")
        request.assert_not_called()

    def test_full_pages_are_not_silently_truncated(self):
        with patch.object(gosplan, "_fetch_with_retry", return_value=[{"purchase_number": "1"}]):
            with self.assertRaises(gosplan.GosplanError):
                list(gosplan.iter_purchases(params={}, page_size=1, max_requests=2, throttle=0))

    def test_offset_limit_splits_fixed_date_window(self):
        params = {"published_after": "2026-11-01T00:00:00+00:00", "published_before": "2026-11-02T00:00:00+00:00"}
        calls = []
        def fetch(query, law):
            calls.append(query)
            if query["published_after"] == params["published_after"] and query["published_before"] == params["published_before"]:
                return [{"purchase_number": str(query["skip"] + i)} for i in range(100)]
            return [{"purchase_number": "boundary-record"}]
        with patch.object(gosplan, "_fetch_with_retry", side_effect=fetch):
            rows = list(gosplan.iter_purchases(params=params, throttle=0))
        self.assertTrue(rows)
        self.assertLessEqual(max(query["skip"] for query in calls), 1000)
        self.assertEqual(len(calls), 13)
