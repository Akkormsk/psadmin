import io
import zipfile
from unittest.mock import patch

from django.test import SimpleTestCase

from . import eis_source, eis_docs


def package(xml):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("export.xml", xml)
    return stream.getvalue()


class DirectXmlSourceTests(SimpleTestCase):
    def test_namespace_and_repeated_items_keep_existing_parser_contract(self):
        rows = eis_source.parse_package([package('''<export xmlns:e="urn:test"><e:epNotificationEF>
          <e:versionNumber>2</e:versionNumber><e:commonInfo><e:purchaseNumber>123</e:purchaseNumber>
          <e:purchaseObjectInfo>Поставка</e:purchaseObjectInfo></e:commonInfo>
          <e:notificationInfo><e:procedureInfo><e:collectingInfo><e:endDT>2026-12-01T00:00:00Z</e:endDT>
          </e:collectingInfo></e:procedureInfo><e:contractConditionsInfo><e:maxPriceInfo>
          <e:maxPrice>500000</e:maxPrice></e:maxPriceInfo></e:contractConditionsInfo></e:notificationInfo>
          </e:epNotificationEF></export>''')])
        with patch.object(eis_source, "records_for", return_value=rows):
            record = eis_source.purchase("123")
        self.assertEqual(record["object_info"], "Поставка")
        self.assertEqual(record["max_price"], "500000")
        self.assertIn("2026-12-01", record["collecting_finished_at"])

    def test_absent_complaints_are_not_invented_as_empty_success(self):
        with patch.object(eis_source, "records_for", return_value=[]):
            with self.assertRaises(eis_docs.EisDocsError):
                eis_source.related("123", "complaints")

    def test_external_entities_are_rejected(self):
        with self.assertRaises(eis_docs.EisDocsError):
            eis_source.parse_package([package('<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>')])

    def test_wrong_registry_number_does_not_create_another_tender(self):
        with patch.object(eis_source, "notification", return_value={"source": {"commonInfo": {"purchaseNumber": "different"}}}):
            with self.assertRaises(eis_docs.EisDocsError):
                eis_source.purchase("123")
