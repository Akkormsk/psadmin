"""Generic, validated CRUD previews. The LLM cannot write models directly."""
from __future__ import annotations

from secrets import token_urlsafe

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.forms.models import model_to_dict
from django.utils import timezone

from .models import AssistantOperation


class CrudError(Exception):
    pass


DENIED_APPS = {"admin", "auth", "contenttypes", "sessions"}
DENIED_MODELS = {"tenders.AssistantOperation", "tenders.OwnerFeedbackEvent", "tenders.OwnerInteraction"}


def _model(label):
    if not isinstance(label, str) or label in DENIED_MODELS or "." not in label:
        raise CrudError("Недоступная модель")
    app_label, model_name = label.split(".", 1)
    if app_label in DENIED_APPS:
        raise CrudError("Недоступная модель")
    try:
        return apps.get_model(app_label, model_name)
    except LookupError as exc:
        raise CrudError("Недоступная модель") from exc


def _writable(model, values, user):
    if not isinstance(values, dict):
        raise CrudError("Некорректные значения")
    fields = {field.name: field for field in model._meta.fields if field.editable and not field.primary_key and not field.auto_created}
    if set(values) - set(fields):
        raise CrudError("Передано недоступное или вычисляемое поле")
    result = dict(values)
    for name in ("created_by", "owner", "actor"):
        field = fields.get(name)
        if field and name not in result and not field.null:
            result[name] = user.pk
    return result


def _snapshot(instance):
    return {key: value for key, value in model_to_dict(instance).items() if key != "id"}


def preview(conversation, user, payload):
    if not user.is_superuser:
        raise CrudError("Для изменения данных через ассистента нужны права администратора")
    if not isinstance(payload, dict) or set(payload) != {"action", "model", "object_id", "values"}:
        raise CrudError("Некорректный план изменения")
    action = payload["action"]
    model = _model(payload["model"])
    object_id = payload["object_id"]
    if action not in {"create", "update", "delete"} or (action == "create") != (object_id is None):
        raise CrudError("Некорректная операция")
    instance = None if action == "create" else model.objects.filter(pk=object_id).first()
    if action != "create" and not instance:
        raise CrudError("Запись не найдена")
    values = _writable(model, payload["values"], user) if action != "delete" else {}
    before = _snapshot(instance) if instance else {}
    candidate = model(**values) if action == "create" else instance
    if action == "update":
        for key, value in values.items():
            setattr(candidate, key, value)
    if action != "delete":
        try:
            candidate.full_clean()
        except ValidationError as exc:
            raise CrudError(exc.message_dict) from exc
    operation = AssistantOperation.objects.create(conversation=conversation, actor=user, action=action, model_label=payload["model"], object_pk=instance.pk if instance else None, values=values, before=before, confirmation_token=token_urlsafe(32))
    return {"operation_id": operation.pk, "token": operation.confirmation_token, "action": action, "model": payload["model"], "object_id": operation.object_pk, "before": before, "after": _snapshot(candidate) if action != "delete" else None}


@transaction.atomic
def confirm(conversation, user, operation_id, token, accept):
    operation = AssistantOperation.objects.select_for_update().filter(pk=operation_id, conversation=conversation, actor=user).first()
    if not operation or operation.confirmation_token != token:
        raise CrudError("Подтверждение не найдено")
    if operation.status != AssistantOperation.STATUS_PREVIEW:
        raise CrudError("Подтверждение уже использовано или устарело")
    if not accept:
        operation.status = AssistantOperation.STATUS_CANCELLED
        operation.save(update_fields=["status"])
        return {"status": operation.status}
    model = _model(operation.model_label)
    instance = model.objects.filter(pk=operation.object_pk).first() if operation.object_pk else None
    if operation.action != "create" and (not instance or _snapshot(instance) != operation.before):
        operation.status = AssistantOperation.STATUS_STALE
        operation.save(update_fields=["status"])
        raise CrudError("Запись изменилась после предпросмотра")
    if operation.action == "create":
        instance = model(**operation.values)
    elif operation.action == "update":
        for key, value in operation.values.items(): setattr(instance, key, value)
    if operation.action == "delete":
        instance.delete()
        result = {"status": "deleted", "object_id": operation.object_pk}
    else:
        instance.full_clean()
        instance.save()
        result = {"status": "executed", "object_id": instance.pk, "after": _snapshot(instance)}
    operation.status = AssistantOperation.STATUS_EXECUTED
    operation.object_pk = operation.object_pk or instance.pk
    operation.executed_at = timezone.now()
    operation.save(update_fields=["status", "object_pk", "executed_at"])
    return result
