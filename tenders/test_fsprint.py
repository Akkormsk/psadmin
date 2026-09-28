import json
import urllib.error
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from .integrations.fsprint import FSPrintError, calculate
from .integrations.fsprint_mapping import apply_quantity


class FSPrintRequestTests(SimpleTestCase):
    """Проверяет, что запрос строится ровно так, как просил пользователь:
    form-urlencoded, без cookies, без браузерных заголовков — только
    Content-Type/Accept/X-Requested-With."""

    def _response(self, body: bytes):
        response = MagicMock()
        response.read.return_value = body
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    @patch("tenders.integrations.fsprint.urlopen")
    def test_sends_form_urlencoded_body_without_cookies_or_browser_headers(self, mock_urlopen):
        mock_urlopen.return_value = self._response(b'{"ok": true}')
        calculate({"product_id": "packet", "tiraj": "1000"})

        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://calc.fsprint.ru/ext/calc/calculate")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded; charset=UTF-8")
        self.assertEqual(request.get_header("X-requested-with"), "XMLHttpRequest")
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("User-agent", request.headers)
        self.assertNotIn("Sec-fetch-mode", request.headers)
        self.assertEqual(request.data, b"product_id=packet&tiraj=1000")

    @patch("tenders.integrations.fsprint.urlopen")
    def test_json_response_is_parsed(self, mock_urlopen):
        mock_urlopen.return_value = self._response(json.dumps({"total": 12345}).encode("utf-8"))
        result = calculate({"product_id": "packet"})
        self.assertEqual(result.raw_json, {"total": 12345})
        self.assertEqual(result.error, "")

    @patch("tenders.integrations.fsprint.urlopen")
    def test_non_json_response_is_not_guessed_at(self, mock_urlopen):
        mock_urlopen.return_value = self._response(b"<html>not json</html>")
        result = calculate({"product_id": "packet"})
        self.assertIsNone(result.raw_json)
        self.assertIn("не JSON", result.error)
        self.assertEqual(result.raw_text, "<html>not json</html>")

    @patch("tenders.integrations.fsprint.urlopen", side_effect=urllib.error.URLError("timed out"))
    def test_network_failure_raises_fsprint_error(self, mock_urlopen):
        with self.assertRaises(FSPrintError):
            calculate({"product_id": "packet"})


class FSPrintMappingTests(SimpleTestCase):
    def test_apply_quantity_sets_tiraj_without_touching_other_fields(self):
        payload = apply_quantity({"product_id": "packet"}, 500)
        self.assertEqual(payload, {"product_id": "packet", "tiraj": "500"})
