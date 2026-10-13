"""Published production knowledge capabilities using existing UI services."""
from .assistant_tools import AssistantTool, AssistantToolError, register_tool
from .models import CounterpartyKnowledgeVersion, Proposal
from .provider_knowledge import confirm_knowledge, confirm_sewing_price_list
from .proposals import apply_batch
from .production_actions import deactivate_lesson


def confirm_provider_knowledge(user, payload):
    version = CounterpartyKnowledgeVersion.objects.filter(
        pk=payload["version_id"], status=CounterpartyKnowledgeVersion.STATUS_DRAFT
    ).first()
    if version is None:
        raise AssistantToolError("Черновик знаний не найден или уже обработан")
    try:
        confirmed = confirm_knowledge(version, user, str(payload.get("note", "")))
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc
    return {
        "counterparty_id": confirmed.counterparty_id,
        "knowledge_version_id": confirmed.pk,
        "status": confirmed.status,
    }


def confirm_sewing_price(user, payload):
    version = CounterpartyKnowledgeVersion.objects.filter(
        pk=payload["version_id"], status=CounterpartyKnowledgeVersion.STATUS_DRAFT,
    ).first()
    if version is None:
        raise AssistantToolError("Черновик прайса не найден или уже обработан")
    try:
        confirmed, binding = confirm_sewing_price_list(
            version, user, str(payload["currency"]), payload["formula_confirmed"] is True,
        )
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc
    return {
        "counterparty_id": confirmed.counterparty_id,
        "knowledge_version_id": confirmed.pk,
        "calculator_binding_id": binding.pk,
        "status": confirmed.status,
    }


def decide_feedback_proposals(user, payload):
    batch_id = payload["batch_id"]
    accepted_ids = payload["accepted_ids"]
    if not isinstance(accepted_ids, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in accepted_ids
    ):
        raise AssistantToolError("Нужен список идентификаторов принимаемых предложений")
    pending = list(Proposal.objects.filter(
        batch_id=batch_id, status=Proposal.STATUS_PENDING
    ).values_list("pk", flat=True))
    if not pending:
        raise AssistantToolError("Нет ожидающих предложений в этом наборе")
    if not set(accepted_ids).issubset(pending):
        raise AssistantToolError("Предложения не принадлежат выбранному набору")
    decided = apply_batch(batch_id, accepted_ids, user)
    return {"decisions": [
        {"id": entry.pk, "status": entry.status, "summary": entry.summary}
        for entry in decided
    ]}


register_tool(AssistantTool(
    "provider.knowledge_confirm", "Подтвердить знания контрагента",
    "Принимает проверенную версию знаний/прайса; выполняет штатную замену прежней версии. Неподтверждённые условия прайса запрещены.",
    {"version_id": "integer", "note": "string?"}, False, True, True,
    confirm_provider_knowledge, source="tenders.provider_knowledge.confirm_knowledge",
))

register_tool(AssistantTool(
    "production.feedback_decide", "Применить исправления производства",
    "Принять выбранные предложения из одной карточки обучения, остальные отклонить по штатным правилам.",
    {"batch_id": "string", "accepted_ids": "integer[]"}, False, True, True,
    decide_feedback_proposals, source="tenders.proposals.apply_batch",
))

register_tool(AssistantTool(
    "provider.sewing_price_confirm", "Подтвердить прайс пошива и активировать калькулятор",
    "После проверки распознанного XLS/XLSX подтверждает валюту и смысл формулы, фиксирует версию прайса и активирует существующий калькулятор.",
    {"version_id": "integer", "currency": "string", "formula_confirmed": "boolean"}, False, True, True,
    confirm_sewing_price, source="tenders.provider_knowledge.confirm_sewing_price_list",
))

def deactivate_training_lesson(user, payload):
    return {"dropped": deactivate_lesson(payload["lesson_id"])}


register_tool(AssistantTool(
    "production.lesson_deactivate", "Отключить ранее выученное правило",
    "Скрыть выбранное правило из следующих производственных расчётов, не удаляя историю обучения.",
    {"lesson_id": "integer"}, False, True, True,
    deactivate_training_lesson, source="tenders.production_actions.deactivate_lesson",
))
