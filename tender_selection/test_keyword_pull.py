from unittest.mock import patch
from django.test import TestCase
from . import gosplan, services


class RunKeywordPullTests(TestCase):
    def test_legacy_second_pass_never_queries_upstream(self):
        with patch.object(gosplan, 'iter_purchases') as network:
            run = services.run_keyword_pull(max_requests=10)
        network.assert_not_called()
        self.assertTrue(run.ok)
        self.assertTrue(run.params['merged_into_primary'])
