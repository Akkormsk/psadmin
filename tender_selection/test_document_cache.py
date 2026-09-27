from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from . import services
from .models import DocumentPreview, Tender

CONTRACT_URL = "https://zakupki.gov.ru/contract"
SPEC_URL = "https://zakupki.gov.ru/spec"
PRICE_URL = "https://zakupki.gov.ru/price"


def _notification(prefix=""):
    def attachment(url, name, kind):
        return {"url": f"{url}{prefix}", "fileName": name, "docKindInfo": {"name": kind}}

    return {"source": {"attachmentsInfo": {"attachmentInfo": [
        attachment(PRICE_URL, "Обоснование НМЦК.xlsx", "Обоснование начальной цены"),
        attachment(CONTRACT_URL, "Проект контракта.docx", "Проект контракта"),
        attachment(SPEC_URL, "Описание объекта закупки.docx", "Описание объекта закупки"),
    ]}}}


def _sufficient():
    return {"data": {"risk_facts": {"documents_sufficient": "true", "execution_days": 90}, "legal_risks": "по контракту"}}


class DocumentHtmlTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="1")

    def test_cached_preview_is_used_without_going_to_eis(self):
        DocumentPreview.objects.create(url=CONTRACT_URL, filename="Проект контракта.docx", html="<p>Пени 1/300</p>")
        with patch.object(services, "_fetch_doc_bytes") as fetch:
            html = services.document_html(self.tender, CONTRACT_URL, "Проект контракта.docx")

        fetch.assert_not_called()
        self.assertEqual(html, "<p>Пени 1/300</p>")

    def test_fetched_document_is_cached_for_next_time(self):
        with patch.object(services, "_fetch_doc_bytes", return_value=b"raw"), \
                patch.object(services, "extract_preview", return_value={"kind": "docx", "html": "<p>текст</p>"}):
            html = services.document_html(self.tender, CONTRACT_URL, "Проект контракта.docx")

        self.assertEqual(html, "<p>текст</p>")
        self.assertEqual(DocumentPreview.objects.get(url=CONTRACT_URL).html, "<p>текст</p>")


class RiskAssessmentDocumentsTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(
            purchase_number="1", review=Tender.INTERESTING, notification_raw=_notification(),
        )
        DocumentPreview.objects.create(url=CONTRACT_URL, filename="Проект контракта.docx", html="<p>Пени 1/300</p>")
        DocumentPreview.objects.create(url=SPEC_URL, filename="Описание объекта закупки.docx", html="<p>Открытки</p>")

    def test_assessment_reads_prefetched_documents(self):
        with patch.object(services, "_fetch_doc_bytes") as fetch, \
                patch("tender_selection.risk_assessment.assess", return_value=_sufficient()) as assess:
            services.risk_assessment_for(self.tender)

        fetch.assert_not_called()
        self.assertIn("Пени 1/300", assess.call_args.args[0])
        self.assertEqual(self.tender.risk_assessment_docs, ["Проект контракта.docx", "Описание объекта закупки.docx"])

    def test_refresh_does_not_replace_full_assessment_with_worse_one(self):
        with patch("tender_selection.risk_assessment.assess", return_value=_sufficient()):
            services.risk_assessment_for(self.tender)
        DocumentPreview.objects.all().delete()

        insufficient = {"data": {"risk_facts": {"documents_sufficient": "false"}, "legal_risks": "без документов"}}
        with patch.object(services, "_fetch_doc_bytes", side_effect=services.DocumentError("ЕИС молчит")), \
                patch("tender_selection.risk_assessment.assess", return_value=insufficient):
            result = services.risk_assessment_for(self.tender, force=True)
        self.tender.refresh_from_db()

        self.assertEqual(result["legal_risks"], "по контракту")
        self.assertEqual(self.tender.risk_assessment["legal_risks"], "по контракту")


class PrefetchDocumentsTests(TestCase):
    def _tender(self, number, *, closes_in, prefix):
        return Tender.objects.create(
            purchase_number=number, notification_raw=_notification(prefix),
            collecting_finished_at=timezone.now() + closes_in,
        )

    def test_prefetches_contract_and_spec_of_open_tenders_nearest_deadline_first(self):
        self._tender("later", closes_in=timedelta(days=5), prefix="?later")
        self._tender("sooner", closes_in=timedelta(days=1), prefix="?sooner")
        self._tender("closed", closes_in=-timedelta(days=1), prefix="?closed")

        with patch.object(services, "document_html", return_value="<p>ok</p>") as read:
            attempted, succeeded = services.retry_pending_documents(limit=3, pause=0)

        fetched = [call.args[1] for call in read.call_args_list]
        self.assertEqual((attempted, succeeded), (3, 3))
        self.assertEqual(fetched, [f"{CONTRACT_URL}?sooner", f"{SPEC_URL}?sooner", f"{CONTRACT_URL}?later"])

    def test_already_cached_documents_are_skipped(self):
        self._tender("sooner", closes_in=timedelta(days=1), prefix="")
        DocumentPreview.objects.create(url=CONTRACT_URL, html="<p>готово</p>")

        with patch.object(services, "document_html", return_value="<p>ok</p>") as read:
            services.retry_pending_documents(limit=5, pause=0)

        self.assertEqual([call.args[1] for call in read.call_args_list], [SPEC_URL])


class UnreadableDocumentTests(TestCase):
    def test_unreadable_download_is_not_fetched_again(self):
        tender = Tender.objects.create(
            purchase_number="1", notification_raw=_notification(), collecting_finished_at=timezone.now() + timedelta(days=1),
        )
        DocumentPreview.objects.create(url=CONTRACT_URL, kind="rar", error="Формат не поддерживается для предпросмотра.")

        with patch.object(services, "_fetch_doc_bytes") as fetch:
            self.assertEqual(services.document_html(tender, CONTRACT_URL, "Проект контракта.rar"), "")
        fetch.assert_not_called()
        with patch.object(services, "document_html", return_value="<p>ok</p>") as read:
            services.retry_pending_documents(limit=5, pause=0)
        self.assertEqual([call.args[1] for call in read.call_args_list], [SPEC_URL])

    def test_archives_are_not_picked_for_risk_assessment(self):
        from .risk_assessment import select_documents

        picked = select_documents([
            {"name": "Проект контракта.rar", "kind": "Проект контракта", "url": "a"},
            {"name": "Проект контракта.docx", "kind": "Проект контракта", "url": "b"},
        ])
        self.assertEqual([doc["url"] for doc in picked], ["b"])
