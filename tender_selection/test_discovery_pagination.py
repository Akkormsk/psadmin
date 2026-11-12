from unittest.mock import patch
from django.test import SimpleTestCase
from . import gosplan


class CompletePaginationTests(SimpleTestCase):
    def test_default_walk_continues_beyond_previous_page_cap(self):
        pages = [[{"purchase_number": str(n)}] for n in range(20)] + [[]]
        with patch.object(gosplan, "_fetch_with_retry", side_effect=pages) as fetch:
            rows = list(gosplan.iter_purchases(params={}, page_size=1, throttle=0))
        self.assertEqual(len(rows), 20)
        self.assertEqual(fetch.call_count, 21)
        self.assertEqual(fetch.call_args.args[0]["skip"], 20)
