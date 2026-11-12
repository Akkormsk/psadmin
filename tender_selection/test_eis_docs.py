from unittest.mock import patch

from django.test import SimpleTestCase

from . import eis_docs


class EisServiceResponseTests(SimpleTestCase):
    def test_service_error_is_reported_and_token_is_redacted(self):
        response = '''<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
          xmlns:e="http://zakupki.gov.ru/fz44/get-docs-ip/ws"><s:Body>
          <e:getDocsByReestrNumberResponse><dataInfo><errorInfo>
          <code>34</code><message>Организация с токеном private-token заблокирована</message>
          </errorInfo></dataInfo></e:getDocsByReestrNumberResponse></s:Body></s:Envelope>'''
        with patch.object(eis_docs, "_soap_call", return_value=response), \
                patch.object(eis_docs, "token", return_value="private-token"):
            with self.assertRaises(eis_docs.EisDocsError) as caught:
                eis_docs.fetch_archive_urls("123")
        message = str(caught.exception)
        self.assertIn("код 34", message)
        self.assertIn("заблокирована", message)
        self.assertNotIn("private-token", message)

    def test_successful_archive_response_is_preserved(self):
        with patch.object(eis_docs, "_soap_call", return_value=
                          '<response><archiveUrl>https://int.zakupki.gov.ru/archive.zip</archiveUrl></response>'):
            self.assertEqual(eis_docs.fetch_archive_urls("123"),
                             ["https://int.zakupki.gov.ru/archive.zip"])

    def test_missing_archive_remains_distinct_from_service_error(self):
        with patch.object(eis_docs, "_soap_call", return_value="<response/>"):
            with self.assertRaisesMessage(eis_docs.EisDocsError, "не вернул архив"):
                eis_docs.fetch_archive_urls("123")

    def test_malformed_response_reports_xml_failure(self):
        with patch.object(eis_docs, "_soap_call", return_value="not XML"):
            with self.assertRaisesMessage(eis_docs.EisDocsError, "некорректный XML"):
                eis_docs.fetch_archive_urls("123")
