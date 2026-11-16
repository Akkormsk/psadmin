"""Local smoke checks for the additive schema and the existing document executor."""
from decimal import Decimal
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase

from tender_selection import services
from tender_selection.models import DocumentPreview, Tender, TenderDocument
from tender_selection.test_document_cache import CONTRACT_URL, _notification
from tenders.models import (
    CalculationComponent, TenderAnalysis, TenderCommercialItem, TenderEstimate, TenderLine, TenderSettings,
)
from tenders.estimate_actions import duplicate_estimate, persist_estimate


class TenderDataSchemaSmokeTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser('data-owner', password='test')
        self.tender = Tender.objects.create(purchase_number='anonymized-print-order', notification_raw=_notification())
        self.product = TenderCommercialItem.objects.create(
            tender=self.tender, source_key='printed-material', display_name='Печатная продукция',
            quantity=Decimal('100'), requirements={'paper': 'мелованная'},
        )

    def test_legacy_document_executor_and_saved_calculation_remain_usable(self):
        preview = DocumentPreview.objects.create(
            url=CONTRACT_URL, filename='Проект контракта.docx', kind='docx', html='<p>Поставка за 10 дней.</p>',
        )
        estimate = TenderEstimate.objects.create(owner=self.owner, tender=self.tender, tender_number='local', name='Печать')
        line = TenderLine.objects.create(estimate=estimate, name=self.product.display_name, quantity=100, nmck_unit=50)
        with patch.object(services, '_fetch_doc_bytes', side_effect=AssertionError('Unexpected network')):
            result = services.preview_tender_document(self.owner, self.tender.pk, 1)
        self.assertEqual(result['html'], preview.html)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.conditions, {})
        self.assertIsNone(self.tender.selected_estimate_id)
        self.assertEqual(estimate.status, TenderEstimate.Status.DRAFT)
        self.assertEqual(line.pk, estimate.lines.get().pk)

    def test_document_facts_are_reused_and_assignments_belong_to_tender(self):
        preview = DocumentPreview.objects.create(
            url=CONTRACT_URL, kind='docx', html='<p>Поставка за 10 дней.</p>',
            content_hash='a' * 64, parser_version='docx',
            extraction={'facts': {'delivery': {'value': '10 дней', 'location': 'paragraph:1'}}},
        )
        link = TenderDocument.objects.create(
            tender=self.tender, preview=preview, source_key=CONTRACT_URL,
            assignments=[{'fact_key': 'delivery', 'scope': 'tender'}],
        )
        other = Tender.objects.create(purchase_number='another-order')
        TenderDocument.objects.create(tender=other, preview=preview, source_key=CONTRACT_URL)
        self.assertEqual(DocumentPreview.objects.count(), 1)
        self.assertEqual(self.tender.document_versions.get(is_current=True).preview.extraction, preview.extraction)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            TenderDocument.objects.create(tender=self.tender, preview=preview, source_key=CONTRACT_URL)
        link.assignments = [{'fact_key': 'delivery', 'scope': 'item', 'target_id': self.product.pk}]
        link.full_clean()
        link.tender = other
        with self.assertRaises(ValidationError):
            link.clean()
        with self.assertRaises(ProtectedError):
            preview.delete()

    def test_analysis_has_one_current_result_per_subject_without_losing_history(self):
        old = TenderAnalysis.objects.create(
            tender=self.tender, commercial_item=self.product, kind='risk',
            input_fingerprint='original', result={'level': 'unknown'}, is_current=False,
        )
        current = TenderAnalysis.objects.create(
            tender=self.tender, commercial_item=self.product, kind='risk',
            input_fingerprint='complete-source', result={'level': 'attention'}, supersedes=old,
        )
        current.full_clean()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            TenderAnalysis.objects.create(tender=self.tender, commercial_item=self.product, kind='risk')
        TenderAnalysis.objects.create(tender=self.tender, kind='risk')
        with transaction.atomic(), self.assertRaises(IntegrityError):
            TenderAnalysis.objects.create(tender=self.tender, kind='risk')
        component = CalculationComponent.objects.create(commercial_item=self.product, name='Обложка')
        TenderAnalysis.objects.create(tender=self.tender, component=component, kind='risk')
        with transaction.atomic(), self.assertRaises(IntegrityError):
            TenderAnalysis.objects.create(tender=self.tender, component=component, kind='risk')
        current.tender = Tender.objects.create(purchase_number='foreign-analysis')
        with self.assertRaises(ValidationError):
            current.clean()
        old.refresh_from_db()
        self.assertEqual(old.result, {'level': 'unknown'})

    def test_calculation_links_keep_snapshot_and_reject_foreign_subject(self):
        component = CalculationComponent.objects.create(commercial_item=self.product, name='Обложка')
        estimate = TenderEstimate.objects.create(
            owner=self.owner, tender=self.tender, tender_number='local', name='Расчёт печати',
        )
        line = TenderLine.objects.create(
            estimate=estimate, component=component, name='Обложка', quantity=100, nmck_unit=50,
            requirements={'paper': 'исходная бумага'},
        )
        line.full_clean()
        self.tender.selected_estimate = estimate
        self.tender.full_clean()
        self.tender.save(update_fields=['selected_estimate'])
        self.product.requirements = {'paper': 'новая бумага'}
        self.product.save(update_fields=['requirements'])
        line.refresh_from_db()
        self.assertEqual(line.requirements, {'paper': 'исходная бумага'})
        other = Tender.objects.create(purchase_number='foreign-calculation')
        estimate.tender = other
        estimate.save(update_fields=['tender'])
        with self.assertRaises(ValidationError):
            line.clean()
        with self.assertRaises(ValidationError):
            self.tender.clean()

    def test_existing_save_and_duplicate_executors_preserve_new_links(self):
        estimate = TenderEstimate.objects.create(owner=self.owner, tender=self.tender, tender_number='local', name='Печать')
        line = TenderLine.objects.create(
            estimate=estimate, commercial_item=self.product, name=self.product.display_name, quantity=100, nmck_unit=50,
        )
        data = {'tender_number': 'local', 'name': 'Печать', 'lines_json': json.dumps([
            {'id': line.pk, 'name': line.name, 'quantity': '100', 'nmck_unit': '50', 'material_unit': '20'},
        ])}
        settings = TenderSettings.objects.create()
        saved, error, _ = persist_estimate(self.owner, data, estimate, settings, pipeline=True)
        self.assertIsNone(error)
        self.assertEqual(saved.lines.get().commercial_item_id, self.product.pk)
        saved.status = TenderEstimate.Status.FINALIZED
        saved.save(update_fields=['status'])
        _, error, _ = persist_estimate(self.owner, data, saved, settings, pipeline=True)
        self.assertIsNotNone(error)
        copied = duplicate_estimate(self.owner, saved.pk, True)
        copy = TenderEstimate.objects.get(pk=copied['estimate_id'])
        self.assertEqual(copy.status, TenderEstimate.Status.DRAFT)
        self.assertEqual(copy.lines.get().commercial_item_id, self.product.pk)


class TenderDataMigrationSmokeTests(TransactionTestCase):
    def test_filled_database_upgrade_repeat_and_rollback_preserve_legacy_data(self):
        previous = [('tender_selection', '0049_primary_search_filters'), ('tenders', '0060_calculation_position_groups')]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        executor.migrate(previous)
        try:
            apps = executor.loader.project_state(previous).apps
            owner = apps.get_model('auth', 'User').objects.create(username='migration-owner')
            tender = apps.get_model('tender_selection', 'Tender').objects.create(
                purchase_number='anonymized-existing-order', notification_raw=_notification(),
            )
            preview = apps.get_model('tender_selection', 'DocumentPreview').objects.create(
                url=CONTRACT_URL, html='<p>Поставка за 10 дней.</p>',
            )
            estimate = apps.get_model('tenders', 'TenderEstimate').objects.create(
                owner_id=owner.pk, tender_id=tender.pk, tender_number='local', name='Сохранённый расчёт',
                summary_snapshot={'is_incomplete': False, 'total': '2000'},
            )
            line = apps.get_model('tenders', 'TenderLine').objects.create(
                estimate_id=estimate.pk, name='Печатная продукция', quantity=100, nmck_unit=50,
                requirements={'paper': 'мелованная'},
            )
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            saved = TenderEstimate.objects.get(pk=estimate.pk)
            self.assertEqual(saved.summary_snapshot, {'is_incomplete': False, 'total': '2000'})
            self.assertEqual(saved.status, TenderEstimate.Status.DRAFT)
            self.assertEqual(TenderLine.objects.get(pk=line.pk).requirements, {'paper': 'мелованная'})
            self.assertEqual(DocumentPreview.objects.get(pk=preview.pk).html, preview.html)
            self.assertEqual(Tender.objects.get(pk=tender.pk).notification_raw, tender.notification_raw)
            self.assertEqual(Tender.objects.get(pk=tender.pk).data_version, Tender.DataVersion.LEGACY)
            self.assertFalse(TenderDocument.objects.exists())
            self.assertFalse(TenderAnalysis.objects.exists())
            MigrationExecutor(connection).migrate(latest)
            self.assertEqual(TenderLine.objects.filter(pk=line.pk).count(), 1)
            executor = MigrationExecutor(connection)
            executor.migrate(previous)
            apps = executor.loader.project_state(previous).apps
            self.assertEqual(apps.get_model('tenders', 'TenderLine').objects.get(pk=line.pk).requirements, {'paper': 'мелованная'})
            self.assertEqual(apps.get_model('tender_selection', 'DocumentPreview').objects.get(pk=preview.pk).html, preview.html)
        finally:
            MigrationExecutor(connection).migrate(latest)
