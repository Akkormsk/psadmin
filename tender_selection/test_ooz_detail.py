from django.test import SimpleTestCase

from .notification import detail_document_candidate


class DetailDocumentCandidateTests(SimpleTestCase):
    def test_offers_ooz_when_notice_has_one_general_position(self):
        document = {"name": "#ООЗ.docx", "kind": "Описание объекта закупки", "index": 2}

        result = detail_document_candidate([{"name": "Услуги по изготовлению печатной продукции"}], [document])

        self.assertEqual(result, document)

    def test_does_not_offer_ooz_when_notice_items_are_already_split(self):
        result = detail_document_candidate(
            [{"name": "Буклет"}, {"name": "Листовка"}],
            [{"name": "ООЗ.docx", "kind": "Описание объекта закупки", "index": 2}],
        )

        self.assertIsNone(result)

    def test_does_not_offer_unrelated_documents(self):
        result = detail_document_candidate(
            [{"name": "Услуги по изготовлению печатной продукции"}],
            [{"name": "Проект контракта.docx", "kind": "Проект контракта", "index": 1}],
        )

        self.assertIsNone(result)
