import os
from unittest.mock import patch

from django.test import SimpleTestCase

from . import scheduler


class TenderAutopullSafetyTests(SimpleTestCase):
    def test_web_autopull_requires_a_separate_explicit_flag(self):
        with patch.dict(os.environ, {"TENDER_AUTOPULL_ENABLED": "1"}, clear=True):
            self.assertFalse(scheduler._web_autopull_enabled())

    def test_web_autopull_starts_only_when_both_flags_are_enabled(self):
        with patch.dict(
            os.environ,
            {
                "TENDER_AUTOPULL_ENABLED": "1",
                "TENDER_AUTOPULL_WEB_ENABLED": "1",
            },
            clear=True,
        ):
            self.assertTrue(scheduler._web_autopull_enabled())
