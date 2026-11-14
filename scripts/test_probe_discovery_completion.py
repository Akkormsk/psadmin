import runpy
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone


class DiscoveryCompletionProbeTests(SimpleTestCase):
    def setUp(self):
        namespace = runpy.run_path(str(Path(__file__).with_name("probe_discovery_completion.py")),
                                  run_name="probe_discovery_completion")
        self.run_probe = namespace["run_probe"]
        self.module = self.run_probe.__globals__
        self.gosplan = self.module["gosplan"]
        self.jobs = [("classifier", "fz44", {"classifier": ["18.12"]})]

    def run_with(self, responses, jobs=None):
        events = []
        with patch.object(self.gosplan, "fetch_page", side_effect=responses) as fetch, \
                patch.dict(self.module, {"emit": lambda event, **fields: events.append({"event": event, **fields})}), \
                patch.object(self.module["time"], "sleep"):
            self.run_probe(jobs or self.jobs, timezone.now())
        return fetch, events[-1]

    def test_full_page_then_429_retries_same_next_page_until_empty(self):
        page = [{"purchase_number": str(number)} for number in range(100)]
        fetch, result = self.run_with([page, self.gosplan.RateLimitError("limit"), []])
        self.assertEqual([call.args[0]["skip"] for call in fetch.call_args_list], [0, 100, 100])
        self.assertTrue(result["complete"])
        self.assertEqual(result["unique_cards"], 100)
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["errors"], {"HTTP429": 1})

    def test_failed_query_does_not_hide_unprocessed_filters(self):
        fetch, result = self.run_with([self.gosplan.GosplanError("network")] * 3 + [[]],
                                      self.jobs * 2)
        self.assertEqual(fetch.call_count, 4)
        self.assertFalse(result["complete"])
        self.assertEqual(result["queries_completed"], 1)
        self.assertEqual(result["queries_not_attempted"], 0)
        self.assertEqual(len(result["incomplete"]), 1)

    def test_test_budget_is_not_reported_as_completed(self):
        with patch.dict(self.module["os"].environ, {"PROBE_MAX_ATTEMPTS": "1"}):
            _, result = self.run_with([self.gosplan.RateLimitError("limit")])
        self.assertFalse(result["complete"])
        self.assertEqual(result["incomplete"][0]["reason"], "probe_budget")
