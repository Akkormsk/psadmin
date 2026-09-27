from django.test import SimpleTestCase

from .risk_policy import classify_risk


class RiskPolicyTests(SimpleTestCase):
    def test_has_no_risk_when_known_facts_have_no_flags(self):
        self.assertEqual(classify_risk({"documents_sufficient": True, "execution_days": 30}), {"risk_level": "low", "risk_factors": []})

    def test_missing_documents_are_not_green(self):
        self.assertEqual(classify_risk({}), {"risk_level": "unknown", "risk_factors": []})

    def test_short_deadline_is_red(self):
        result = classify_risk({"documents_sufficient": True, "execution_days": 7})
        self.assertEqual(result["risk_level"], "high")
        self.assertEqual(result["risk_factors"][0]["code"], "short_deadline")

    def test_warning_and_red_factors_use_red(self):
        result = classify_risk({"documents_sufficient": True, "execution_days": 10, "delivery_mode": "requests_open_ended"})
        self.assertEqual(result["risk_level"], "high")
        self.assertEqual(len(result["risk_factors"]), 2)

    def test_short_contract_deadline_says_it_is_the_whole_contract(self):
        result = classify_risk({"documents_sufficient": True, "execution_days": 7})
        self.assertIn("всего контракта", result["risk_factors"][0]["text"])

    def test_short_per_request_deadline_is_named_as_such(self):
        result = classify_risk({
            "documents_sufficient": True, "execution_days": 90, "batch_days": 7, "delivery_mode": "requests_with_end",
        })
        batch = next(f for f in result["risk_factors"] if f["code"] == "short_batch")
        self.assertEqual(batch["level"], "high")
        self.assertIn("по одной заявке 7 дн.", batch["text"])
        self.assertNotIn("short_deadline", [f["code"] for f in result["risk_factors"]])
