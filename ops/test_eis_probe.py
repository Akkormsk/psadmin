import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from ops.eis_probe import check_endpoint


class CheckEndpointTests(unittest.TestCase):
    @patch("ops.eis_probe.urlopen")
    def test_http_error_means_endpoint_is_reachable(self, urlopen):
        urlopen.side_effect = HTTPError(
            "https://example.test", 403, "Forbidden", {}, None
        )

        result = check_endpoint("eis", "https://example.test")

        self.assertTrue(result["reachable"])
        self.assertEqual(result["http_status"], 403)

    @patch("ops.eis_probe.urlopen")
    def test_network_error_means_endpoint_is_not_reachable(self, urlopen):
        urlopen.side_effect = URLError("timed out")

        result = check_endpoint("eis", "https://example.test")

        self.assertFalse(result["reachable"])
        self.assertEqual(result["error_type"], "URLError")
