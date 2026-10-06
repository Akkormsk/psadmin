from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .cascade import Cascade
from .models import Step4DecisionCache
from .step4_decision_cache import Step4Decision, Step4DecisionWrite, bulk_store_step4_decisions
from .test_cascade import _product


@override_settings(
    STEP4_DECISION_CACHE_ENABLED=True,
    STEP4_DECISION_CACHE_STALE_DAYS=30,
    STEP4_DECISION_CACHE_CONTRACT_VERSION="step4-v1",
)
class Step4DecisionCacheIntegrationTests(TestCase):
    def setUp(self):
        self.pool = [_product(f"Флешка {number}", external_id=str(number)) for number in range(100)]
        self.cascade = Cascade({"name": "Флешка"})
        self.cascade.item = "флешка"

    def store(self, products, decision):
        bulk_store_step4_decisions(
            target=self.cascade.item,
            supplier=products[0].supplier_id,
            decisions=[
                Step4DecisionWrite(product.external_id, product.full_name or product.name, decision, "gemini-test")
                for product in products
            ],
        )

    def _execute_step4(self, pool=None, keep=None):
        pool = pool or self.pool
        with patch("tenders.services._run_name_filter", return_value=set(keep or [])) as run:
            result = self.cascade.step_4_name_filter(pool)
        return result, run

    def test_empty_cache_sends_all_candidates_to_gemini(self):
        _result, run = self._execute_step4(keep=[product.external_id for product in self.pool])
        self.assertEqual(len(run.call_args.args[1]), 100)
        self.assertEqual(self.cascade.diagnostics["step4_gemini_candidates"], 100)

    def test_full_cache_skips_gemini_and_applies_pass_reject(self):
        self.store(self.pool[:50], Step4Decision.PASS)
        self.store(self.pool[50:], Step4Decision.REJECT)
        result, run = self._execute_step4()
        self.assertFalse(run.called)
        self.assertEqual([product.external_id for product in result], [product.external_id for product in self.pool[:50]])

    def test_mixed_cache_sends_only_misses_to_gemini(self):
        self.store(self.pool[:80], Step4Decision.PASS)
        result, run = self._execute_step4(keep=[product.external_id for product in self.pool[80:]])
        self.assertEqual(len(run.call_args.args[1]), 20)
        self.assertEqual([product.external_id for product in result], [product.external_id for product in self.pool])

    def test_changed_name_misses_cache(self):
        self.store([self.pool[0]], Step4Decision.PASS)
        self.pool[0].full_name = "Другая флешка"
        _result, run = self._execute_step4(pool=[self.pool[0]], keep=["0"])
        self.assertEqual(run.call_args.args[1], [("0", "Другая флешка")])

    def test_stale_entry_revalidates_and_updates_timestamp(self):
        self.store([self.pool[0]], Step4Decision.PASS)
        record = Step4DecisionCache.objects.get()
        record.last_verified_at = timezone.now() - timedelta(days=31)
        record.save(update_fields=["last_verified_at"])
        with patch("tenders.services._run_name_filter", side_effect=lambda *_args, **kwargs: (kwargs["on_valid_batch"]([("0", self.pool[0].full_name)], set()), {"0"})[1]) as run:
            self.cascade.step_4_name_filter([self.pool[0]])
        record.refresh_from_db()
        self.assertTrue(run.called)
        self.assertGreater(record.last_verified_at, timezone.now() - timedelta(days=1))

    def test_malformed_result_does_not_write_cache(self):
        self._execute_step4(pool=[self.pool[0]], keep=["0"])
        self.assertFalse(Step4DecisionCache.objects.exists())

    def test_provider_failure_keeps_candidate_and_does_not_write_cache(self):
        with patch("tenders.services._run_name_filter", return_value=None):
            result = self.cascade.step_4_name_filter([self.pool[0]])
        self.assertEqual(result, [self.pool[0]])
        self.assertFalse(Step4DecisionCache.objects.exists())

    def test_mixed_results_keep_original_order(self):
        self.store([self.pool[1]], Step4Decision.REJECT)
        self.store([self.pool[3]], Step4Decision.PASS)
        result, _run = self._execute_step4(pool=self.pool[:5], keep=["0", "2", "4"])
        self.assertEqual([product.external_id for product in result], ["0", "2", "3", "4"])

    @override_settings(STEP4_DECISION_CACHE_ENABLED=False)
    def test_feature_flag_off_uses_legacy_path(self):
        with patch("tenders.services._run_name_filter", return_value={"0"}) as run:
            result = self.cascade.step_4_name_filter([self.pool[0], self.pool[1]])
        self.assertTrue(run.called)
        self.assertEqual([product.external_id for product in result], ["0"])

    def test_full_cache_lookup_has_no_n_plus_one(self):
        self.store(self.pool, Step4Decision.PASS)
        with CaptureQueriesContext(connection) as queries:
            result, run = self._execute_step4()
        self.assertFalse(run.called)
        self.assertEqual(len(result), 100)
        self.assertLessEqual(len(queries), 2)

    def test_empty_cache_matches_legacy_order(self):
        pool = self.pool[:5]
        with patch("tenders.services._run_name_filter", return_value={"0", "2", "4"}):
            new_result = self.cascade.step_4_name_filter(pool)
        with override_settings(STEP4_DECISION_CACHE_ENABLED=False):
            legacy = Cascade({"name": "Флешка"})
            legacy.item = "флешка"
            with patch("tenders.services._run_name_filter", return_value={"0", "2", "4"}):
                old_result = legacy.step_4_name_filter(pool)
        self.assertEqual(
            [product.external_id for product in new_result],
            [product.external_id for product in old_result],
        )
