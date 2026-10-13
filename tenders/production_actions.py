"""Backend production-knowledge actions shared by UI and assistant."""
from django.db import transaction

from .models import Lesson, ProductionTrainingExample, Proposal, Counterparty, ProcessDefinition, TenderKnowledgeSource
from .proposals import apply_proposal


def deactivate_lesson(lesson_id):
    """Deactivate the learned instruction without deleting historical evidence."""
    return bool(Lesson.objects.filter(pk=lesson_id, is_active=True).update(is_active=False))


@transaction.atomic
def deactivate_route_knowledge(payload):
    """Disable both learned route representations, mirroring the UI."""
    if not isinstance(payload, dict):
        raise ValueError("Не удалось определить сохранённый опыт")
    if payload.get("example_id") not in (None, ""):
        try:
            example_id = int(payload["example_id"])
        except (ValueError, TypeError):
            raise ValueError("Неверный идентификатор маршрута")
        changed = ProductionTrainingExample.objects.filter(pk=example_id).update(is_active=False)
        Lesson.objects.filter(
            session__confirmed_example_id=example_id,
            scope__in=["route", "production_step"],
        ).update(is_active=False)
        return bool(changed)
    try:
        lesson_id = int(payload["lesson_id"])
    except (ValueError, TypeError, KeyError):
        raise ValueError("Не указан идентификатор знания")
    lesson = Lesson.objects.filter(
        pk=lesson_id, scope__in=["route", "production_step"],
    ).first()
    if lesson is None:
        raise ValueError("Сохранённый опыт не найден")
    lesson.is_active = False
    lesson.save(update_fields=["is_active"])
    if lesson.session_id and lesson.session.confirmed_example_id:
        ProductionTrainingExample.objects.filter(
            pk=lesson.session.confirmed_example_id,
        ).update(is_active=False)
    return True


@transaction.atomic
def confirm_counterparty_with_stages(user, payload):
    """Commit one approved production-base counterparty with stage links."""
    if not isinstance(payload, dict):
        raise ValueError("Не удалось прочитать форму контрагента")
    name = str(payload.get("name", "")).strip()[:200]
    if not name:
        raise ValueError("Не указано название контрагента")
    notes = str(payload.get("notes", ""))[:2000]
    raw_ids = payload.get("stage_ids", [])
    if not isinstance(raw_ids, list) or not all(str(x).isdigit() for x in raw_ids):
        raise ValueError("Некорректные производственные этапы")
    stage_ids = list(dict.fromkeys(int(x) for x in raw_ids))
    stages = list(ProcessDefinition.objects.filter(pk__in=stage_ids))
    if len(stages) != len(stage_ids):
        raise ValueError("Один или несколько этапов не найдены")
    proposal = Proposal.objects.create(
        type=Proposal.TYPE_CREATE_COUNTERPARTY,
        payload={"name": name, "notes": notes},
        summary=f"Новый контрагент «{name}»", created_by=user,
    )
    apply_proposal(proposal, user)
    counterparty = Counterparty.objects.get(name=name)
    for stage in stages:
        link = Proposal.objects.create(
            type=Proposal.TYPE_LINK_STAGE_COUNTERPARTY, batch_id=proposal.batch_id,
            payload={"stage_id": stage.pk, "counterparty_id": counterparty.pk},
            summary=f"Связать «{stage.name}» и «{name}»", created_by=user,
        )
        apply_proposal(link, user)
    if payload.get("source_id"):
        TenderKnowledgeSource.objects.filter(
            pk=payload["source_id"], counterparty__isnull=True,
        ).update(counterparty=counterparty)
    return {
        "id": counterparty.pk, "name": counterparty.name,
        "stage_names": list(counterparty.stage_links.select_related("stage").values_list("stage__name", flat=True)),
    }
