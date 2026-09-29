import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from .integrations import fsprint
from .integrations.fsprint import FSPrintError, calculate
from .integrations.fsprint_mapping import apply_quantity

SAMPLE_SHOW_VARIANT_PATH = Path(__file__).resolve().parent / "integrations" / "fsprint_sample_show_variant_response.html"
SAMPLE_SHOW_VARIANT_BLOCKNOTE_PATH = Path(__file__).resolve().parent / "integrations" / "fsprint_sample_show_variant_response_blocknote.html"

# Реконструкция вида ответа /calculate по факту, задокументированному в
# docs/backlog/fsprint-adapter.md ("Номер расчёта: <span id="jir_nom">...")
# — не полный сырой захват (его не сохраняли), только то, что нужно для
# проверки извлечения номера расчёта.
SAMPLE_CALCULATE_RESPONSE = (
    'Номер расчёта: <span id="jir_nom">1547916</span> от 28.09.2026'
    "<script>$.post('/ext/calc/show_variant', {record: 1547916, var: 0, curr: 'RUR', usetemp: 0}, function(){});</script>"
)


class FSPrintRequestTests(SimpleTestCase):
    """Проверяет, что запрос строится ровно так, как просил пользователь:
    form-urlencoded, без cookies, без браузерных заголовков — только
    Content-Type/Accept/X-Requested-With."""

    def setUp(self):
        fsprint._session_cookies.clear()

    def _response(self, body: bytes, set_cookie: list = None):
        response = MagicMock()
        response.read.return_value = body
        response.headers.get_all.return_value = set_cookie or []
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    @patch("tenders.integrations.fsprint.urlopen")
    def test_calculate_posts_form_urlencoded_body_without_cookies_or_browser_headers(self, mock_urlopen):
        mock_urlopen.side_effect = [
            self._response(SAMPLE_CALCULATE_RESPONSE.encode("utf-8")),
            self._response(SAMPLE_SHOW_VARIANT_PATH.read_bytes()),
        ]
        calculate({"product_id": "packet", "tiraj": "1000"})

        request = mock_urlopen.call_args_list[0][0][0]
        self.assertEqual(request.full_url, "https://calc.fsprint.ru/ext/calc/calculate")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded; charset=UTF-8")
        self.assertEqual(request.get_header("X-requested-with"), "XMLHttpRequest")
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("User-agent", request.headers)
        self.assertEqual(request.data, b"product_id=packet&tiraj=1000")

    @patch("tenders.integrations.fsprint.urlopen")
    def test_show_variant_is_requested_with_the_record_from_calculate(self, mock_urlopen):
        mock_urlopen.side_effect = [
            self._response(SAMPLE_CALCULATE_RESPONSE.encode("utf-8")),
            self._response(SAMPLE_SHOW_VARIANT_PATH.read_bytes()),
        ]
        calculate({"product_id": "packet"})

        request = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(request.full_url, "https://calc.fsprint.ru/ext/calc/show_variant")
        self.assertEqual(request.data, b"record=1547916&var=0&curr=RUR&usetemp=0")

    @patch("tenders.integrations.fsprint.urlopen")
    def test_real_show_variant_sample_is_parsed_into_timeline_options_and_fields(self, mock_urlopen):
        mock_urlopen.side_effect = [
            self._response(SAMPLE_CALCULATE_RESPONSE.encode("utf-8")),
            self._response(SAMPLE_SHOW_VARIANT_PATH.read_bytes()),
        ]
        result = calculate({"product_id": "packet", "tiraj": "1000"})

        self.assertEqual(result.record, "1547916")
        self.assertEqual(result.error, "")
        self.assertEqual(len(result.timeline_options), 6)
        base = result.timeline_options[0]
        self.assertIn("базовый срок", base.label)
        self.assertEqual(base.markup_percent, 0.0)
        self.assertEqual(base.total_cost, 74515.99)
        self.assertEqual(base.price_per_unit, 74.52)
        fastest = result.timeline_options[-1]
        self.assertEqual(fastest.markup_percent, 350.0)
        self.assertEqual(fastest.price_per_unit, 335.32)
        self.assertIn(("Тип продукции", "Пакеты"), result.fields)
        self.assertIn(("Тираж", "1000"), result.fields)

    @patch("tenders.integrations.fsprint.urlopen")
    def test_real_blocknote_sample_confirms_the_format_is_not_specific_to_one_product(self, mock_urlopen):
        """Второй реальный продукт (свои сроки/подписи, 3 блока вместо 1) —
        проверяет, что парсер не подогнан под частности «Пакетов»."""
        mock_urlopen.side_effect = [
            self._response(SAMPLE_CALCULATE_RESPONSE.encode("utf-8")),
            self._response(SAMPLE_SHOW_VARIANT_BLOCKNOTE_PATH.read_bytes()),
        ]
        result = calculate({"product_id": "blocknote", "block_count": "3"})

        self.assertEqual(result.error, "")
        self.assertEqual(len(result.timeline_options), 6)
        base = result.timeline_options[0]
        self.assertIn("Обычная", base.label)
        self.assertEqual(base.markup_percent, 0.0)
        self.assertEqual(base.total_cost, 60884.98)
        self.assertEqual(base.price_per_unit, 60.88)
        self.assertIn(("Тип продукции", "Блокноты"), result.fields)

    @patch("tenders.integrations.fsprint.urlopen")
    def test_server_issued_cookie_is_remembered_and_resent_on_the_next_request(self, mock_urlopen):
        """Сайт FSPrint после нескольких подряд запросов БЕЗ сессии начинает
        требовать логин (см. docs/backlog/fsprint-adapter.md) — держим
        только то, что сервер сам вернул в Set-Cookie, не чужие значения."""
        mock_urlopen.side_effect = [
            self._response(SAMPLE_CALCULATE_RESPONSE.encode("utf-8"), set_cookie=["mojolicious=abc123; Path=/; HttpOnly"]),
            self._response(SAMPLE_SHOW_VARIANT_PATH.read_bytes()),
        ]
        calculate({"product_id": "packet"})

        first_request = mock_urlopen.call_args_list[0][0][0]
        self.assertNotIn("Cookie", first_request.headers)
        second_request = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(second_request.get_header("Cookie"), "mojolicious=abc123")

        mock_urlopen.side_effect = [
            self._response(SAMPLE_CALCULATE_RESPONSE.encode("utf-8")),
            self._response(SAMPLE_SHOW_VARIANT_PATH.read_bytes()),
        ]
        calculate({"product_id": "packet"})
        third_request = mock_urlopen.call_args_list[2][0][0]
        self.assertEqual(third_request.get_header("Cookie"), "mojolicious=abc123")

    @patch("tenders.integrations.fsprint.urlopen")
    def test_missing_record_in_calculate_response_is_reported_not_guessed(self, mock_urlopen):
        mock_urlopen.return_value = self._response("<html>совсем другой формат</html>".encode("utf-8"))
        result = calculate({"product_id": "packet"})
        self.assertEqual(result.record, "")
        self.assertIn("не найден номер расчёта", result.error)
        mock_urlopen.assert_called_once()

    @patch("tenders.integrations.fsprint.urlopen", side_effect=urllib.error.URLError("timed out"))
    def test_network_failure_raises_fsprint_error(self, mock_urlopen):
        with self.assertRaises(FSPrintError):
            calculate({"product_id": "packet"})


class FSPrintMappingTests(SimpleTestCase):
    def test_apply_quantity_sets_tiraj_without_touching_other_fields(self):
        payload = apply_quantity({"product_id": "packet"}, 500)
        self.assertEqual(payload, {"product_id": "packet", "tiraj": "500"})
