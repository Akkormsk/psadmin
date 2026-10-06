from datetime import timedelta

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .models import CatalogSupplier, Step4DecisionCache
from .step4_decision_cache import (
    Step4Decision,
    Step4DecisionWrite,
    Step4ProductCandidate,
    bulk_lookup_step4_decisions,
    bulk_store_step4_decisions,
    candidate_signature,
    normalize_target_signature,
)


@override_settings(
    STEP4_DECISION_CACHE_ENABLED=True,
    STEP4_DECISION_CACHE_STALE_DAYS=30,
    STEP4_DECISION_CACHE_CONTRACT_VERSION="step4-v1",
)
class Step4DecisionCacheTests(TestCase):
    def setUp(self):
        self.supplier = CatalogSupplier.objects.create(name="Oasis")
        self.target = "Термокружка"
        self.candidate = Step4ProductCandidate("100", "Термокружка 500 мл")

    def store(self, decision=Step4Decision.PASS, *, target=None, candidate=None, contract_version="step4-v1"):
        candidate = candidate or self.candidate
        bulk_store_step4_decisions(
            target=target or self.target,
            supplier=self.supplier,
            contract_version=contract_version,
            decisions=[Step4DecisionWrite(candidate.external_id, candidate.candidate_name, decision, "gemini-test")],
        )

    def lookup(self, *, target=None, candidate=None, contract_version="step4-v1"):
        candidate = candidate or self.candidate
        return bulk_lookup_step4_decisions(
            target=target or self.target,
            supplier=self.supplier,
            contract_version=contract_version,
            candidates=[candidate],
        )

    def test_identical_identity_is_a_hit(self):
        self.store()
        self.assertEqual(self.lookup().hits["100"].decision, Step4Decision.PASS)

    def test_changed_candidate_name_is_a_miss(self):
        self.store()
        self.assertEqual(self.lookup(candidate=Step4ProductCandidate("100", "Термокружка 350 мл")).hits, {})

    def test_changed_target_is_a_miss(self):
        self.store()
        self.assertEqual(self.lookup(target="Бутылка для воды").hits, {})

    def test_changed_contract_version_is_a_miss(self):
        self.store()
        self.assertEqual(self.lookup(contract_version="step4-v2").hits, {})

    def test_stale_row_is_a_miss_without_deletion(self):
        self.store()
        record = Step4DecisionCache.objects.get()
        record.last_verified_at = timezone.now() - timedelta(days=31)
        record.save(update_fields=["last_verified_at"])
        result = self.lookup()
        self.assertEqual(result.hits, {})
        self.assertEqual(result.stale_external_ids, {"100"})
        self.assertTrue(Step4DecisionCache.objects.filter(pk=record.pk).exists())

    def test_pass_and_reject_are_stored(self):
        self.store(Step4Decision.PASS)
        self.assertEqual(Step4DecisionCache.objects.get().decision, Step4Decision.PASS)
        self.store(Step4Decision.REJECT)
        self.assertEqual(Step4DecisionCache.objects.get().decision, Step4Decision.REJECT)

    def test_duplicate_upsert_leaves_one_row(self):
        self.store()
        self.store()
        self.assertEqual(Step4DecisionCache.objects.count(), 1)

    def test_target_normalization_is_deterministic(self):
        self.assertEqual(
            normalize_target_signature("  ТЕРМОКРУЖКА\u00a0 500 МЛ  "),
            normalize_target_signature("термокружка 500 мл"),
        )

    def test_candidate_signature_is_deterministic(self):
        self.assertEqual(candidate_signature("Термокружка 500 мл"), candidate_signature("Термокружка 500 мл"))
        self.assertNotEqual(candidate_signature("Термокружка 500 мл"), candidate_signature("Термокружка 350 мл"))

    def test_bulk_lookup_has_no_n_plus_one(self):
        candidates = [self.candidate, Step4ProductCandidate("101", "Термокружка 350 мл")]
        bulk_store_step4_decisions(
            target=self.target,
            supplier=self.supplier,
            decisions=[
                Step4DecisionWrite("100", "Термокружка 500 мл", Step4Decision.PASS, "gemini-test"),
                Step4DecisionWrite("101", "Термокружка 350 мл", Step4Decision.REJECT, "gemini-test"),
            ],
        )
        with CaptureQueriesContext(connection) as queries:
            result = bulk_lookup_step4_decisions(target=self.target, supplier=self.supplier, candidates=candidates)
        self.assertEqual(set(result.hits), {"100", "101"})
        self.assertLessEqual(len(queries), 2)
