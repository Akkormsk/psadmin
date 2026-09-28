from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from .models import Counterparty, ProcessDefinition, Proposal, StageCounterpartyLink, TenderKnowledgeSource


class StageFlagBackfillTests(TestCase):
    """Проверяет саму формулу переноса role → supplies_input/performs_production/
    terminal_mode на живых объектах — данные для реальных строк переносит
    tenders.migrations.0046, здесь только фиксируем ожидаемую раскладку."""

    def test_supply_role_supplies_input_and_is_not_terminal_by_default(self):
        stage = ProcessDefinition.objects.create(name="Закупка готового изделия", role=ProcessDefinition.ROLE_SUPPLY)
        stage.supplies_input, stage.performs_production, stage.terminal_mode = True, False, ProcessDefinition.TERMINAL_SOMETIMES
        stage.save()
        stage.refresh_from_db()
        self.assertTrue(stage.supplies_input)
        self.assertFalse(stage.performs_production)

    def test_completion_role_is_always_terminal(self):
        stage = ProcessDefinition.objects.create(
            name="Упаковка (тест)", role=ProcessDefinition.ROLE_COMPLETION,
            supplies_input=False, performs_production=False, terminal_mode=ProcessDefinition.TERMINAL_ALWAYS,
        )
        self.assertEqual(stage.terminal_mode, ProcessDefinition.TERMINAL_ALWAYS)


class CounterpartyAndLinkTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="admin", password="password")
        self.stage = ProcessDefinition.objects.create(name="УФ-печать", role=ProcessDefinition.ROLE_PRODUCTION, performs_production=True)

    def test_one_stage_can_have_several_counterparties(self):
        a = Counterparty.objects.create(name="Типография А", created_by=self.user)
        b = Counterparty.objects.create(name="Типография Б", created_by=self.user)
        StageCounterpartyLink.objects.create(stage=self.stage, counterparty=a, price_source_type=StageCounterpartyLink.SOURCE_MANUAL_QUOTE)
        StageCounterpartyLink.objects.create(stage=self.stage, counterparty=b, price_source_type=StageCounterpartyLink.SOURCE_HISTORICAL)
        self.assertEqual(self.stage.counterparty_links.count(), 2)

    def test_one_counterparty_can_serve_several_stages(self):
        other_stage = ProcessDefinition.objects.create(name="Тампопечать", role=ProcessDefinition.ROLE_PRODUCTION, performs_production=True)
        counterparty = Counterparty.objects.create(name="Универсальный подрядчик", created_by=self.user)
        StageCounterpartyLink.objects.create(stage=self.stage, counterparty=counterparty)
        StageCounterpartyLink.objects.create(stage=other_stage, counterparty=counterparty)
        self.assertEqual(counterparty.stage_links.count(), 2)

    def test_same_pair_cannot_be_linked_twice(self):
        counterparty = Counterparty.objects.create(name="Типография А", created_by=self.user)
        StageCounterpartyLink.objects.create(stage=self.stage, counterparty=counterparty)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StageCounterpartyLink.objects.create(stage=self.stage, counterparty=counterparty)


class KnowledgeSourceRawFileTests(TestCase):
    def test_source_can_hold_a_raw_screenshot_linked_to_a_counterparty(self):
        user = get_user_model().objects.create_superuser(username="admin", password="password")
        counterparty = Counterparty.objects.create(name="FSPrint", created_by=user)
        source = TenderKnowledgeSource.objects.create(
            title="Переписка в Telegram", source_type="image", counterparty=counterparty,
            raw_file=b"\x89PNG\r\n...", raw_file_name="chat.png", raw_file_content_type="image/png",
            created_by=user,
        )
        source.refresh_from_db()
        self.assertEqual(bytes(source.raw_file), b"\x89PNG\r\n...")
        self.assertEqual(source.counterparty, counterparty)

    def test_reparsed_source_keeps_the_old_one_as_superseded(self):
        user = get_user_model().objects.create_superuser(username="admin", password="password")
        old = TenderKnowledgeSource.objects.create(title="Прайс v1", source_type="text", created_by=user, is_active=False)
        new = TenderKnowledgeSource.objects.create(title="Прайс v2", source_type="text", created_by=user, superseded_by=None)
        old.superseded_by = new
        old.save(update_fields=["superseded_by"])
        old.refresh_from_db()
        self.assertEqual(old.superseded_by, new)
        self.assertFalse(old.is_active)


class ProposalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="admin", password="password")

    def test_proposal_defaults_to_pending_and_changes_nothing_by_itself(self):
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_COUNTERPARTY,
            payload={"name": "Сималенд"},
            summary="Новый контрагент «Сималенд»",
            created_by=self.user,
        )
        self.assertEqual(proposal.status, Proposal.STATUS_PENDING)
        self.assertEqual(Counterparty.objects.count(), 0)

    def test_manual_edit_can_be_recorded_as_already_accepted(self):
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_UPDATE_STAGE,
            payload={"scope_tags": ["бумажные пакеты"]},
            summary="Добавить «бумажные пакеты» в область применения",
            status=Proposal.STATUS_ACCEPTED,
            created_by=self.user,
            decided_by=self.user,
        )
        self.assertEqual(proposal.status, Proposal.STATUS_ACCEPTED)

    def test_several_proposals_from_one_feedback_share_a_batch(self):
        batch_id = Proposal.objects.create(
            type=Proposal.TYPE_UPDATE_STAGE, payload={}, summary="Дополнить область применения этапа", created_by=self.user,
        ).batch_id
        Proposal.objects.create(
            type=Proposal.TYPE_CREATE_LESSON, payload={}, summary="Сохранить урок", created_by=self.user, batch_id=batch_id,
        )
        Proposal.objects.create(
            type=Proposal.TYPE_LINK_STAGE_COUNTERPARTY, payload={}, summary="Подтвердить связь с FSPrint", created_by=self.user, batch_id=batch_id,
        )
        self.assertEqual(Proposal.objects.filter(batch_id=batch_id).count(), 3)


class ProductionBaseDataViewTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(username="admin", password="password")
        self.manager = get_user_model().objects.create_user(username="manager", password="password")

    def test_ordinary_manager_is_refused(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("tender_production_base"))
        self.assertEqual(response.status_code, 403)

    def test_admin_sees_stages_and_linked_counterparties(self):
        stage = ProcessDefinition.objects.create(
            name="УФ-печать (вью-тест)", role=ProcessDefinition.ROLE_PRODUCTION,
            performs_production=True, scope_tags=["сувениры"],
        )
        counterparty = Counterparty.objects.create(name="Типография Вью", created_by=self.admin)
        StageCounterpartyLink.objects.create(stage=stage, counterparty=counterparty, price_source_type="manual_quote")
        self.client.force_login(self.admin)
        data = self.client.get(reverse("tender_production_base")).json()
        stage_row = next(row for row in data["stages"] if row["name"] == "УФ-печать (вью-тест)")
        self.assertEqual(stage_row["counterparty_count"], 1)
        self.assertEqual(stage_row["scope_tags"], ["сувениры"])
        counterparty_row = next(row for row in data["counterparties"] if row["name"] == "Типография Вью")
        self.assertEqual(counterparty_row["stage_names"], ["УФ-печать (вью-тест)"])
