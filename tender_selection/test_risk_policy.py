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
