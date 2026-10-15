"""Backend production-knowledge actions shared by UI and assistant."""
from django.db import transaction

from .cascade_settings import text_search_settings
from .models import CascadeConfigVersion, CascadeLabPreset, Lesson, ProductionTrainingExample, ProductionTrainingSession, Proposal, Counterparty, ProcessDefinition, RequirementSkipRule, TenderKnowledgeSource
from .proposals import apply_proposal


def _require_admin(user):
    if not getattr(user, "is_superuser", False):
        raise ValueError("Действие доступно только администратору")


def save_cascade_lab_preset(user, name, settings):
    """Save the same named Cascade Lab preset as the settings button."""
    _require_admin(user)
    name = str(name or "").strip()[:200]
    if not name:
        raise ValueError("Введите название набора настроек.")
    preset, created = CascadeLabPreset.objects.update_or_create(
        created_by=user, name=name, defaults={"settings": text_search_settings(settings)},
    )
    return {"id": preset.pk, "name": preset.name, "settings": preset.settings, "created": created}


@transaction.atomic
def activate_cascade_lab_settings(user, name, settings):
    """Activate one Cascade configuration with the exact UI side effects."""
    _require_admin(user)
    name = str(name or "Текущие настройки").strip()[:200]
    CascadeConfigVersion.objects.filter(is_active=True).update(is_active=False)
    version = CascadeConfigVersion.objects.create(
        name=name, settings=text_search_settings(settings), created_by=user, is_active=True,
    )
    return {"id": version.pk, "name": version.name, "created_at": version.created_at.isoformat()}


def restore_requirement_to_route(user, label):
    """Undo the same learned requirement exclusion as the route UI button."""
    _require_admin(user)
    from .services import _normalized_text

    label_normalized = _normalized_text(str(label or ""))[:200]
    if not label_normalized:
        raise ValueError("Не удалось определить строку ТЗ.")
    removed = RequirementSkipRule.objects.filter(label_normalized=label_normalized).update(is_active=False)
    return {"dropped": bool(removed)}


@transaction.atomic
def confirm_proposed_stage(user, session_id, proposal_id):
    """Create one approved route stage, exactly as the route panel does."""
    _require_admin(user)
    session = ProductionTrainingSession.objects.select_for_update().filter(
        pk=session_id, created_by=user, is_confirmed=False,
    ).first()
    if session is None:
        raise ValueError("Этап не найден или маршрут устарел. Обновите расчёт.")
    hypothesis = session.current_hypothesis if isinstance(session.current_hypothesis, dict) else {}
    proposals = hypothesis.get("stage_proposals", [])
    proposed = next((item for item in proposals if isinstance(item, dict) and item.get("id") == proposal_id), None)
    if not isinstance(proposed, dict):
        raise ValueError("Этап не найден или маршрут устарел. Обновите расчёт.")
    role = proposed.get("role")
    stage_payload = {
        "name": proposed.get("name"), "description": proposed.get("description", ""),
        "supplies_input": role == "supply", "performs_production": role == "production",
        "terminal_mode": "always" if role == "completion" else "sometimes",
    }
    proposal = Proposal.objects.create(
        type=Proposal.TYPE_CREATE_STAGE, payload=stage_payload,
        summary=f"Новый этап «{stage_payload['name']}» — предложен из маршрута", created_by=user,
    )
    apply_proposal(proposal, user)
    hypothesis["stage_proposals"] = [item for item in proposals if item.get("id") != proposal_id]
    session.current_hypothesis = hypothesis
    session.save(update_fields=["current_hypothesis", "updated_at"])
    return {**hypothesis, "session_id": session.pk, "route_refresh_required": True}


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

@transaction.atomic
def draft_production_feedback(user, text):
    """Prepare one reviewable proposal batch; no global changes are applied."""
    from .services import parse_production_feedback
    from .proposals import payload_from_feedback_item

    text = str(text).strip()
    if not text:
        raise ValueError("Напишите, что изменить")
    stages = list(ProcessDefinition.objects.filter(is_active=True).values("id", "name"))
    counterparties = list(Counterparty.objects.filter(is_active=True).values("id", "name"))
    items = parse_production_feedback(text, stages, counterparties)
    batch_id = None
    proposals = []
    for item in items:
        proposal = Proposal.objects.create(
            type=item["type"], payload=payload_from_feedback_item(item["type"], item["raw"]),
            summary=item["summary"], source_text=text, created_by=user,
            **({"batch_id": batch_id} if batch_id else {}),
        )
        batch_id = batch_id or proposal.batch_id
        proposals.append(proposal)
    return {
        "batch_id": str(batch_id) if batch_id else None,
        "items": [{"id": p.pk, "type": p.type, "type_display": p.get_type_display(),
                   "summary": p.summary, "payload": p.payload} for p in proposals],
        "active_stages": stages, "active_counterparties": counterparties,
    }
