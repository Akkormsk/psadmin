from unittest.mock import patch

from django.test import TestCase

from .models import FilterSettings, Tender, TenderDismissalFeedback


class ProfileTriageTests(TestCase):
    def test_explainable_profile_verdicts_preserve_manual_status(self):
        from .profile_triage import triage_tenders

        settings = FilterSettings.load()
        settings.profile_triage_enabled = True
        settings.save(update_fields=["profile_triage_enabled"])
        clear = Tender.objects.create(purchase_number="1", title="Поставка сувенирной продукции")
        doubt = Tender.objects.create(purchase_number="2", title="Изготовление брендированных материалов")
        unrelated = Tender.objects.create(purchase_number="3", title="Поставка полиграфического оборудования")
        with patch("tender_selection.profile_triage.chat_json", return_value={"data": {"items": [
            {"id": clear.pk, "verdict": "fit", "confidence": 0.92, "reason": "Сувенирная продукция входит в профиль.", "basis": [{"field": "title", "quote": "Поставка сувенирной продукции"}]},
            {"id": doubt.pk, "verdict": "review", "confidence": 0.51, "reason": "Нужен состав продукции."},
            {"id": unrelated.pk, "verdict": "not_fit", "confidence": 0.08, "reason": "Оборудование, а не полиграфическая продукция.", "basis": [{"field": "title", "quote": "Поставка полиграфического оборудования"}]},
        ]}, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}):
            triage_tenders([clear.pk, doubt.pk, unrelated.pk])

        clear.refresh_from_db()
        doubt.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(clear.profile_signal, Tender.PROFILE_SIGNAL_CLEAR)
        self.assertEqual(doubt.profile_signal, Tender.PROFILE_SIGNAL_DOUBT)
        self.assertEqual(unrelated.profile_signal, Tender.PROFILE_SIGNAL_NOT_PROFILE)
        self.assertIsNotNone(unrelated.profile_checked_at)
        self.assertEqual(unrelated.profile_model, "anthropic/claude-haiku-5-5")
        self.assertIn("Оборудование", unrelated.profile_reason)
        self.assertEqual(unrelated.status, Tender.NEW)

    def test_invalid_agent_item_is_left_unchecked_for_safe_retry(self):
        from .profile_triage import triage_tenders

        settings = FilterSettings.load()
        settings.profile_triage_enabled = True
        settings.save(update_fields=["profile_triage_enabled"])
        tender = Tender.objects.create(purchase_number="bad-agent", title="Поставка пакетов для стерилизации")
        with patch("tender_selection.profile_triage.chat_json", return_value={"data": {"items": [
            {"id": tender.pk, "verdict": "other", "confidence": "unknown"},
        ]}, "usage": {}}):
            self.assertEqual(triage_tenders([tender.pk]), 0)

        tender.refresh_from_db()
        self.assertIsNone(tender.profile_checked_at)

    def test_dismiss_reason_is_recorded_only_for_an_incoming_tender(self):
        from django.contrib.auth import get_user_model

        admin = get_user_model().objects.create_superuser("admin", password="x")
        tender = Tender.objects.create(purchase_number="4", title="Полиграфическое оборудование")
        self.client.force_login(admin)

        response = self.client.post(f"/tender-selection/{tender.pk}/dismiss/", {"reason": "not_profile"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(TenderDismissalFeedback.objects.get(tender=tender).reason, TenderDismissalFeedback.NOT_PROFILE)


    def test_confirmed_foreign_is_hidden_without_deleting_and_can_be_seen_with_show_all(self):
        from .services import visible_found_tenders
        from .profile_triage import CONTRACT
        settings = FilterSettings.load(); settings.profile_triage_enabled = True; settings.save()
        tender = Tender.objects.create(purchase_number='evidence-hidden', title='Чужой предмет',
            profile_assessment={'contract': CONTRACT, 'title': 'Чужой предмет', 'object_info': '', 'verdict': 'not_fit'})
        kwargs = {'min_price': 0, 'include_words': '', 'exclude_words': ''}
        rows, hidden, _ = visible_found_tenders(Tender.objects.filter(pk=tender.pk), **kwargs)
        self.assertEqual(rows, []); self.assertEqual(hidden, 1)
        rows, _, _ = visible_found_tenders(Tender.objects.filter(pk=tender.pk), show_all=True, **kwargs)
        self.assertEqual([row.pk for row in rows], [tender.pk])
        tender.review = Tender.INTERESTING; tender.save()
        rows, _, _ = visible_found_tenders(Tender.objects.filter(pk=tender.pk), **kwargs)
        self.assertEqual([row.pk for row in rows], [tender.pk])

    def test_changed_title_invalidates_old_profile_exclusion(self):
        from .profile_triage import CONTRACT, profile_disposition
        tender = Tender(title='Новое описание', object_info='', profile_assessment={
            'contract': CONTRACT, 'title': 'Прежнее описание', 'object_info': '', 'verdict': 'not_fit'})
        self.assertEqual(profile_disposition(tender), 'review')

    def test_foreign_verdict_must_account_for_every_actual_item(self):
        from .profile_triage import _state, _valid_answer
        from tenders.test_calculation_v2_pipeline import notification, raw_item
        tender = Tender.objects.create(purchase_number='mixed-evidence', title='Разные изделия',
            notification_raw=notification([raw_item('Предмет А', '1'), raw_item('Предмет Б', '1')]))
        state = _state(tender, FilterSettings.load())
        answer = {'id': tender.pk, 'verdict': 'not_fit', 'confidence': .01, 'reason': 'Не соответствует профилю',
            'basis': [{'field': 'objects', 'index': 0, 'quote': 'Предмет А'}]}
        self.assertIsNone(_valid_answer(answer, {tender.pk: state}))
        answer['basis'].append({'field': 'objects', 'index': 1, 'quote': 'Предмет Б'})
        self.assertIsNotNone(_valid_answer(answer, {tender.pk: state}))
        answer['basis'][0]['quote'] = 'Несуществующий факт'
        self.assertIsNone(_valid_answer(answer, {tender.pk: state}))

    def test_dismiss_retains_uploaded_document_preview(self):
        from .models import DocumentPreview
        from .services import clear_tender_document_previews
        tender = Tender.objects.create(purchase_number='retain-evidence', title='Изделие')
        preview = DocumentPreview.objects.create(url='https://example.test/manual-file', html='<p>Ручная замена</p>')
        clear_tender_document_previews(tender)
        self.assertTrue(DocumentPreview.objects.filter(pk=preview.pk).exists())

    def test_source_word_exclusion_does_not_overrule_verified_fit(self):
        from .services import visible_found_tenders
        from .profile_triage import CONTRACT
        settings = FilterSettings.load(); settings.profile_triage_enabled = True; settings.save()
        tender = Tender.objects.create(purchase_number='verified-positive', title='Описание закупки',
            profile_assessment={'contract': CONTRACT, 'title': 'Описание закупки', 'object_info': '', 'verdict': 'fit'})
        rows, _, _ = visible_found_tenders(Tender.objects.filter(pk=tender.pk), min_price=0,
            include_words='другое', exclude_words='описание')
        self.assertEqual([row.pk for row in rows], [tender.pk])

    def test_profile_uses_verified_structured_reference_without_model_copying_text(self):
        from .profile_triage import _state, _valid_answer
        tender = Tender.objects.create(purchase_number='structured-reference', title='Подтверждённый предмет')
        state = _state(tender, FilterSettings.load())
        answer = {'id': tender.pk, 'verdict': 'fit', 'confidence': .85, 'reason': 'Подходит по фактическому предмету',
            'basis': [{'field': 'title'}]}
        valid = _valid_answer(answer, {tender.pk: state})
        self.assertEqual(valid[1], Tender.PROFILE_SIGNAL_CLEAR)
        self.assertEqual(valid[-1]['basis'][0]['quote'], tender.title)
