from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Counterparty, Lesson, ProcessDefinition, Proposal, StageCounterpartyLink
from .proposals import apply_batch, apply_proposal, reject_proposal
from .services import TenderAIError


class CreateStageProposalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="admin", password="password")

    def test_accepting_creates_a_stage_with_derived_role_and_no_role_in_payload(self):
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_STAGE,
            payload={"name": "Литьё силикона", "performs_production": True, "scope_tags": ["браслеты"]},
            summary="Новый этап «Литьё силикона»",
            created_by=self.user,
        )
        apply_proposal(proposal, self.user)
        stage = ProcessDefinition.objects.get(name="Литьё силикона")
        self.assertEqual(stage.role, ProcessDefinition.ROLE_PRODUCTION)
        self.assertTrue(stage.performs_production)
        self.assertEqual(stage.scope_tags, ["браслеты"])
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, Proposal.STATUS_ACCEPTED)
        self.assertEqual(proposal.decided_by, self.user)
        self.assertIsNotNone(proposal.decided_at)

    def test_colliding_name_updates_the_existing_stage_instead_of_duplicating(self):
        ProcessDefinition.objects.create(name="Тампопечать (тест)", role=ProcessDefinition.ROLE_PRODUCTION, performs_production=True)
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_STAGE,
            payload={"name": "Тампопечать (тест)", "performs_production": True, "when_to_use": "На мелких предметах"},
            summary="Уточнить «Тампопечать (тест)»",
            created_by=self.user,
        )
        apply_proposal(proposal, self.user)
        self.assertEqual(ProcessDefinition.objects.filter(name="Тампопечать (тест)").count(), 1)
        self.assertEqual(ProcessDefinition.objects.get(name="Тампопечать (тест)").when_to_use, "На мелких предметах")

    def test_missing_name_leaves_proposal_pending(self):
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_STAGE, payload={}, summary="Пустое предложение", created_by=self.user,
        )
        with self.assertRaises(TenderAIError):
            apply_proposal(proposal, self.user)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, Proposal.STATUS_PENDING)


class UpdateStageProposalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="admin", password="password")
        self.stage = ProcessDefinition.objects.create(
            name="Универсальная типография (тест)", role=ProcessDefinition.ROLE_PRODUCTION,
            performs_production=True, scope_tags=["папки", "каталоги"],
        )

    def test_scope_tags_add_merges_without_duplicating(self):
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_UPDATE_STAGE,
            payload={"stage_id": self.stage.pk, "fields": {"scope_tags_add": ["бумажные пакеты", "папки"]}},
            summary="Добавить «бумажные пакеты» в область применения",
            created_by=self.user,
        )
        apply_proposal(proposal, self.user)
        self.stage.refresh_from_db()
        self.assertEqual(sorted(self.stage.scope_tags), ["бумажные пакеты", "каталоги", "папки"])

    def test_stage_name_fallback_resolves_when_id_is_absent(self):
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_UPDATE_STAGE,
            payload={"stage_name": self.stage.name, "fields": {"when_not_to_use": "Мелкий тираж"}},
            summary="Уточнить когда не использовать",
            created_by=self.user,
        )
        apply_proposal(proposal, self.user)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.when_not_to_use, "Мелкий тираж")

    def test_unknown_stage_name_leaves_proposal_pending(self):
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_UPDATE_STAGE,
            payload={"stage_name": "Не существует", "fields": {"when_to_use": "x"}},
            summary="x", created_by=self.user,
        )
        with self.assertRaises(TenderAIError):
            apply_proposal(proposal, self.user)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, Proposal.STATUS_PENDING)


class RejectProposalTests(TestCase):
    def test_rejecting_changes_nothing_but_the_proposal_itself(self):
        user = get_user_model().objects.create_superuser(username="admin", password="password")
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_COUNTERPARTY, payload={"name": "Сималенд"},
            summary="Новый контрагент «Сималенд»", created_by=user,
        )
        reject_proposal(proposal, user)
        self.assertEqual(Counterparty.objects.count(), 0)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, Proposal.STATUS_REJECTED)
        self.assertEqual(proposal.decided_by, user)

    def test_deciding_an_already_decided_proposal_is_a_no_op(self):
        user = get_user_model().objects.create_superuser(username="admin", password="password")
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_COUNTERPARTY, payload={"name": "Сималенд"},
            summary="x", created_by=user,
        )
        apply_proposal(proposal, user)
        self.assertEqual(Counterparty.objects.count(), 1)
        first_decided_at = proposal.decided_at
        # Повторное применение того же (уже принятого) Proposal не должно
        # создать вторую запись и не должно упасть — двойной клик в UI безопасен.
        apply_proposal(proposal, user)
        self.assertEqual(Counterparty.objects.count(), 1)
        self.assertEqual(proposal.decided_at, first_decided_at)


class LinkStageCounterpartyProposalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="admin", password="password")
        self.stage = ProcessDefinition.objects.create(name="УФ-печать (тест)", role=ProcessDefinition.ROLE_PRODUCTION, performs_production=True)

    def test_link_by_ids(self):
        counterparty = Counterparty.objects.create(name="Типография А", created_by=self.user)
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_LINK_STAGE_COUNTERPARTY,
            payload={"stage_id": self.stage.pk, "counterparty_id": counterparty.pk, "price_source_type": "manual_quote"},
            summary="Связать «УФ-печать (тест)» и «Типография А»", created_by=self.user,
        )
        apply_proposal(proposal, self.user)
        link = StageCounterpartyLink.objects.get(stage=self.stage, counterparty=counterparty)
        self.assertEqual(link.price_source_type, "manual_quote")

    def test_missing_counterparty_leaves_proposal_pending(self):
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_LINK_STAGE_COUNTERPARTY,
            payload={"stage_id": self.stage.pk, "counterparty_name": "Ещё не создан"},
            summary="x", created_by=self.user,
        )
        with self.assertRaises(TenderAIError):
            apply_proposal(proposal, self.user)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, Proposal.STATUS_PENDING)


class ApplyBatchTests(TestCase):
    """Один фидбэк → несколько независимых предложений в одной карточке
    (§24): создание контрагента и связь с этапом в одном батче, контрагент
    в связи находится по имени, потому что batch применяет сущности раньше
    связей (см. _BATCH_ORDER в proposals.py)."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="admin", password="password")
        self.stage = ProcessDefinition.objects.create(name="Универсальная типография (батч)", role=ProcessDefinition.ROLE_PRODUCTION, performs_production=True)

    def test_accepting_the_whole_batch_creates_counterparty_and_link_and_lesson(self):
        create_counterparty = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_COUNTERPARTY, payload={"name": "FSPrint"},
            summary="Новый контрагент «FSPrint»", created_by=self.user,
        )
        batch_id = create_counterparty.batch_id
        link = Proposal.objects.create(
            type=Proposal.TYPE_LINK_STAGE_COUNTERPARTY,
            payload={"stage_id": self.stage.pk, "counterparty_name": "FSPrint"},
            summary="Связать с «Универсальная типография (батч)»", created_by=self.user, batch_id=batch_id,
        )
        lesson = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_LESSON,
            payload={"scope": "route", "admin_text": "Бумажные пакеты — через универсальную типографию"},
            summary="Сохранить урок", created_by=self.user, batch_id=batch_id,
        )
        apply_batch(batch_id, {create_counterparty.pk, link.pk, lesson.pk}, self.user)
        self.assertTrue(Counterparty.objects.filter(name="FSPrint").exists())
        self.assertTrue(StageCounterpartyLink.objects.filter(stage=self.stage, counterparty__name="FSPrint").exists())
        self.assertEqual(Lesson.objects.filter(source=Lesson.SOURCE_PROPOSAL).count(), 1)

    def test_unselected_items_in_the_batch_are_rejected_not_left_pending(self):
        keep = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_COUNTERPARTY, payload={"name": "Оставляем"},
            summary="x", created_by=self.user,
        )
        drop = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_COUNTERPARTY, payload={"name": "Отклоняем"},
            summary="x", created_by=self.user, batch_id=keep.batch_id,
        )
        apply_batch(keep.batch_id, {keep.pk}, self.user)
        keep.refresh_from_db()
        drop.refresh_from_db()
        self.assertEqual(keep.status, Proposal.STATUS_ACCEPTED)
        self.assertEqual(drop.status, Proposal.STATUS_REJECTED)
        self.assertFalse(Counterparty.objects.filter(name="Отклоняем").exists())


class CreateLessonProposalTests(TestCase):
    def test_lesson_from_a_proposal_is_tagged_with_that_source(self):
        user = get_user_model().objects.create_superuser(username="admin", password="password")
        proposal = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_LESSON,
            payload={"scope": "production_step", "admin_text": "Считать с белилами и двумя проходами", "item_word": "кружка"},
            summary="Сохранить урок про белила", created_by=user,
        )
        apply_proposal(proposal, user)
        lesson = Lesson.objects.get(item_word="кружка")
        self.assertEqual(lesson.source, Lesson.SOURCE_PROPOSAL)
        self.assertEqual(lesson.scope, "production_step")
