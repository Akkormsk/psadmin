"""Применение подтверждённых предложений «Базы производства».

Proposal сам по себе ничего не меняет — это только запись «что предложено».
Функции этого модуля — единственное место, где подтверждённое предложение
превращается в реальную детерминированную правку (создать/обновить Stage,
Counterparty, связь между ними или Lesson). Ни одна функция не решает,
*можно ли* пользователю подтверждать — эту проверку (сейчас: только
суперпользователь, см. docs/claude-production-base-prompt.md §44) делает
вызывающий код (view), не сервисный слой.

Ручные правки внутри «Базы производства» тоже идут через `apply_proposal`,
просто с уже созданным `Proposal(status=accepted)` — так вся история
глобальных изменений проходит одним путём и Proposal заодно служит её
журналом (см. docs/claude-production-base-prompt.md §35, §38)."""

from django.db import transaction
from django.utils import timezone

from .models import Counterparty, Lesson, ProcessDefinition, Proposal, StageCounterpartyLink
from .services import TenderAIError, _cell_text


STAGE_EDITABLE_FIELDS = {
    "name", "description", "supplies_input", "performs_production", "terminal_mode",
    "scope_tags", "when_to_use", "when_not_to_use", "parameters", "is_active",
}
COUNTERPARTY_EDITABLE_FIELDS = {"name", "notes", "is_active", "catalog_supplier_id"}
LINK_EDITABLE_FIELDS = {"price_source_type", "priority", "settings", "is_active"}


def _derive_legacy_role(payload):
    """`role` остаётся техническим полем для кода, который его ещё читает
    (`routes.py`, экспорт знаний) — новый экран не спрашивает его у
    администратора, а выводит из тех же флагов, что видит пользователь."""
    if payload.get("performs_production"):
        return ProcessDefinition.ROLE_PRODUCTION
    if payload.get("supplies_input"):
        return ProcessDefinition.ROLE_SUPPLY
    if payload.get("terminal_mode") == ProcessDefinition.TERMINAL_ALWAYS:
        return ProcessDefinition.ROLE_COMPLETION
    return ProcessDefinition.ROLE_PRODUCTION


def _resolve_stage(payload):
    stage_id = payload.get("stage_id")
    if stage_id:
        try:
            return ProcessDefinition.objects.get(pk=stage_id)
        except ProcessDefinition.DoesNotExist:
            raise TenderAIError("Этап из предложения больше не существует.")
    name = _cell_text(payload.get("stage_name"))
    if not name:
        raise TenderAIError("В предложении не указан этап.")
    stage = ProcessDefinition.objects.filter(name=name).first()
    if stage is None:
        raise TenderAIError(f"Этап «{name}» не найден — сначала подтвердите его создание.")
    return stage


def _resolve_counterparty(payload):
    counterparty_id = payload.get("counterparty_id")
    if counterparty_id:
        try:
            return Counterparty.objects.get(pk=counterparty_id)
        except Counterparty.DoesNotExist:
            raise TenderAIError("Контрагент из предложения больше не существует.")
    name = _cell_text(payload.get("counterparty_name"))
    if not name:
        raise TenderAIError("В предложении не указан контрагент.")
    counterparty = Counterparty.objects.filter(name=name).first()
    if counterparty is None:
        raise TenderAIError(f"Контрагент «{name}» не найден — сначала подтвердите его создание.")
    return counterparty


def payload_from_feedback_item(item_type, raw):
    """Свёртывает один разобранный ИИ пункт свободного фидбэка (см.
    services.parse_production_feedback) в payload ровно того вида, что
    ожидают обработчики выше — те же поля-белые-списки, что и у ручного
    экрана. Незнакомые/лишние ключи из ответа ИИ отбрасываются здесь, не
    доходят до записи в базу."""
    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    if item_type == Proposal.TYPE_CREATE_STAGE:
        return {"name": _cell_text(raw.get("name")), "description": _cell_text(raw.get("description"))}
    if item_type == Proposal.TYPE_UPDATE_STAGE:
        return {"stage_name": _cell_text(raw.get("stage_name")), "fields": {k: v for k, v in fields.items() if k in STAGE_EDITABLE_FIELDS}}
    if item_type == Proposal.TYPE_CREATE_COUNTERPARTY:
        return {"name": _cell_text(raw.get("name")), "notes": _cell_text(raw.get("notes"))}
    if item_type == Proposal.TYPE_UPDATE_COUNTERPARTY:
        return {"counterparty_name": _cell_text(raw.get("counterparty_name")), "fields": {k: v for k, v in fields.items() if k in COUNTERPARTY_EDITABLE_FIELDS}}
    if item_type == Proposal.TYPE_LINK_STAGE_COUNTERPARTY:
        payload = {"stage_name": _cell_text(raw.get("stage_name")), "counterparty_name": _cell_text(raw.get("counterparty_name"))}
        if isinstance(raw.get("priority"), int):
            payload["priority"] = raw["priority"]
        return payload
    if item_type == Proposal.TYPE_CREATE_LESSON:
        return {"admin_text": _cell_text(raw.get("admin_text"))}
    return {}


def _apply_create_stage(proposal, user):
    payload = proposal.payload
    name = _cell_text(payload.get("name"))
    if not name:
        raise TenderAIError("В предложении не указано название этапа.")
    fields = {key: payload[key] for key in STAGE_EDITABLE_FIELDS if key in payload and key != "name"}
    try:
        stage, created = ProcessDefinition.objects.get_or_create(
            name=name, defaults={"role": _derive_legacy_role(payload), **fields},
        )
    except ProcessDefinition.MultipleObjectsReturned:
        raise TenderAIError(f"У этапа «{name}» уже несколько записей в справочнике — правьте вручную.")
    if not created:
        for key, value in fields.items():
            setattr(stage, key, value)
        stage.save()
    return stage


def _apply_update_stage(proposal, user):
    payload = proposal.payload
    stage = _resolve_stage(payload)
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    scope_add = fields.get("scope_tags_add")
    changed = []
    for key, value in fields.items():
        if key in STAGE_EDITABLE_FIELDS:
            setattr(stage, key, value)
            changed.append(key)
    if isinstance(scope_add, list) and scope_add:
        stage.scope_tags = sorted({*stage.scope_tags, *(_cell_text(value) for value in scope_add if _cell_text(value))})
        changed.append("scope_tags")
    if changed:
        stage.save()
    return stage


def _apply_create_counterparty(proposal, user):
    payload = proposal.payload
    name = _cell_text(payload.get("name"))
    if not name:
        raise TenderAIError("В предложении не указано название контрагента.")
    fields = {}
    if "notes" in payload:
        fields["notes"] = _cell_text(payload.get("notes"))
    if payload.get("catalog_supplier_id"):
        fields["catalog_supplier_id"] = payload["catalog_supplier_id"]
    counterparty, created = Counterparty.objects.get_or_create(
        name=name, defaults={"created_by": proposal.created_by, **fields},
    )
    if not created:
        for key, value in fields.items():
            setattr(counterparty, key, value)
        counterparty.save()
    return counterparty


def _apply_update_counterparty(proposal, user):
    payload = proposal.payload
    counterparty = _resolve_counterparty(payload)
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    changed = False
    for key, value in fields.items():
        if key in COUNTERPARTY_EDITABLE_FIELDS:
            setattr(counterparty, key, value)
            changed = True
    if changed:
        counterparty.save()
    return counterparty


def _apply_link_stage_counterparty(proposal, user):
    payload = proposal.payload
    stage = _resolve_stage(payload)
    counterparty = _resolve_counterparty(payload)
    fields = {key: payload[key] for key in LINK_EDITABLE_FIELDS if key in payload}
    link, created = StageCounterpartyLink.objects.get_or_create(
        stage=stage, counterparty=counterparty, defaults=fields,
    )
    if not created:
        for key, value in fields.items():
            setattr(link, key, value)
        link.save()
    return link


def _apply_create_lesson(proposal, user):
    payload = proposal.payload
    admin_text = _cell_text(payload.get("admin_text")) or _cell_text(proposal.source_text)
    if not admin_text:
        raise TenderAIError("В предложении нет текста урока.")
    return Lesson.objects.create(
        scope=payload.get("scope") if payload.get("scope") in dict(Lesson.SCOPE_CHOICES) else "route",
        admin_text=admin_text,
        summary=_cell_text(payload.get("summary"))[:300],
        item_word=_cell_text(payload.get("item_word"))[:120],
        tz_labels=payload.get("tz_labels") if isinstance(payload.get("tz_labels"), list) else [],
        production_type=_cell_text(payload.get("production_type"))[:120],
        outcome=payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {},
        source=Lesson.SOURCE_PROPOSAL,
        session_id=proposal.session_id,
        created_by=user,
    )


_HANDLERS = {
    Proposal.TYPE_CREATE_STAGE: _apply_create_stage,
    Proposal.TYPE_UPDATE_STAGE: _apply_update_stage,
    Proposal.TYPE_CREATE_COUNTERPARTY: _apply_create_counterparty,
    Proposal.TYPE_UPDATE_COUNTERPARTY: _apply_update_counterparty,
    Proposal.TYPE_LINK_STAGE_COUNTERPARTY: _apply_link_stage_counterparty,
    Proposal.TYPE_CREATE_LESSON: _apply_create_lesson,
}

# Внутри одной карточки (batch) сущности применяются раньше связей и уроков,
# чтобы «Связать этап и контрагента» и «Сохранить урок» могли найти по имени
# то, что создано этим же батчем секундой раньше (см. §24 промпта).
_BATCH_ORDER = {
    Proposal.TYPE_CREATE_STAGE: 0,
    Proposal.TYPE_CREATE_COUNTERPARTY: 0,
    Proposal.TYPE_UPDATE_STAGE: 1,
    Proposal.TYPE_UPDATE_COUNTERPARTY: 1,
    Proposal.TYPE_LINK_STAGE_COUNTERPARTY: 2,
    Proposal.TYPE_CREATE_LESSON: 3,
}


@transaction.atomic
def apply_proposal(proposal, user):
    """Выполняет предложенную правку и помечает Proposal принятым.

    Идемпотентно: повторный вызов на уже решённом Proposal ничего не делает
    и не бросает исключение — так UI может не бояться двойного клика."""
    if proposal.status != Proposal.STATUS_PENDING:
        return proposal
    handler = _HANDLERS.get(proposal.type)
    if handler is None:
        raise TenderAIError(f"Неизвестный тип предложения: {proposal.type}")
    handler(proposal, user)
    proposal.status = Proposal.STATUS_ACCEPTED
    proposal.decided_by = user
    proposal.decided_at = timezone.now()
    proposal.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    return proposal


def reject_proposal(proposal, user):
    """Отклоняет предложение — глобальных данных не трогает вовсе."""
    if proposal.status != Proposal.STATUS_PENDING:
        return proposal
    proposal.status = Proposal.STATUS_REJECTED
    proposal.decided_by = user
    proposal.decided_at = timezone.now()
    proposal.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    return proposal


def apply_batch(batch_id, accepted_ids, user):
    """Разбирает одну карточку («несколько независимых пунктов из одного
    фидбэка», §24): пункты из `accepted_ids` — принимает, остальные пункты
    этого же batch_id — отклоняет. Каждый пункт решается отдельной записью
    Proposal, ничего не оставляет «зависшим» в pending."""
    proposals = list(Proposal.objects.filter(batch_id=batch_id, status=Proposal.STATUS_PENDING))
    proposals.sort(key=lambda item: _BATCH_ORDER.get(item.type, 9))
    accepted_ids = set(accepted_ids)
    return [
        apply_proposal(proposal, user) if proposal.pk in accepted_ids else reject_proposal(proposal, user)
        for proposal in proposals
    ]
