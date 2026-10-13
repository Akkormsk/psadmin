"""Published production knowledge capabilities using existing UI services."""
from .assistant_tools import AssistantTool, AssistantToolError, register_tool
from .models import CounterpartyKnowledgeVersion, Proposal
from .provider_knowledge import confirm_knowledge, confirm_sewing_price_list
from .proposals import apply_batch
from .production_actions import deactivate_lesson, deactivate_route_knowledge, confirm_counterparty_with_stages
from .estimate_actions import duplicate_estimate, delete_estimate, set_tender_estimate_status, set_order_estimate_status, EstimateActionError


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
    from uuid import UUID
    try:
        batch_id = str(UUID(str(payload["batch_id"])))
    except (ValueError, TypeError):
        raise AssistantToolError("Неверный идентификатор набора предложений")
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

def manage_saved_estimate(user, payload, *, delete=False):
    pipeline = payload.get("pipeline", True)
    if not isinstance(pipeline, bool):
        raise AssistantToolError("Тип расчёта должен быть указан однозначно")
    try:
        executor = delete_estimate if delete else duplicate_estimate
        return executor(user, payload["estimate_id"], pipeline)
    except EstimateActionError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool(
    "estimate.duplicate", "Копировать сохранённый тендерный или заказной расчёт",
    "Создаёт самостоятельную копию со всеми строками через ту же операцию, что используется на странице расчёта.",
    {"estimate_id": "integer", "pipeline": "boolean?"}, False, True, True,
    lambda user, payload: manage_saved_estimate(user, payload),
    source="tenders.estimate_actions.duplicate_estimate",
))
register_tool(AssistantTool(
    "estimate.delete", "Удалить сохранённый тендерный или заказной расчёт",
    "Удаляет выбранный расчёт через штатный механизм с проверкой владельца; операция необратима.",
    {"estimate_id": "integer", "pipeline": "boolean?"}, False, True, True,
    lambda user, payload: manage_saved_estimate(user, payload, delete=True),
    source="tenders.estimate_actions.delete_estimate",
))

def set_estimate_outcome(user, payload, *, pipeline):
    try:
        executor = set_tender_estimate_status if pipeline else set_order_estimate_status
        return executor(user, payload["estimate_id"], payload["status"])
    except EstimateActionError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool(
    "estimate.tender_status", "Изменить пользовательскую стадию тендерного расчёта",
    "Устанавливает доступную стадию расчёта и при отказе от участия архивирует тендер через штатный сервис. Не заменяет получение официального протокола торгов.",
    {"estimate_id": "integer", "status": "string"}, False, True, True,
    lambda user, payload: set_estimate_outcome(user, payload, pipeline=True),
    source="tenders.estimate_actions.set_tender_estimate_status",
))
register_tool(AssistantTool(
    "estimate.order_status", "Изменить стадию отдельного заказа",
    "Меняет статус заказа и при необходимости создаёт связанную карточку заказа так же, как существующая страница.",
    {"estimate_id": "integer", "status": "string"}, False, True, True,
    lambda user, payload: set_estimate_outcome(user, payload, pipeline=False),
    source="tenders.estimate_actions.set_order_estimate_status",
))

def deactivate_route_experience(user, payload):
    try:
        return {"dropped": deactivate_route_knowledge(payload)}
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool(
    "production.route_knowledge_deactivate", "Отключить неверный производственный маршрут",
    "Отключает выбранный подтверждённый маршрут и связанные с ним выученные правила по тому же алгоритму, что и интерфейс.",
    {"lesson_id": "integer?", "example_id": "integer?"}, False, True, True,
    deactivate_route_experience, source="tenders.production_actions.deactivate_route_knowledge",
))

def confirm_production_counterparty(user, payload):
    try:
        return confirm_counterparty_with_stages(user, payload)
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool(
    "provider.production_create", "Добавить контрагента и производственные связи",
    "Создаёт или обновляет контрагента и связывает его с выбранными этапами через штатный журнал предложений; использует тот же исполнитель, что и «База производства».",
    {"name": "string", "notes": "string?", "stage_ids": "integer[]", "source_id": "integer?"},
    False, True, True, confirm_production_counterparty,
    source="tenders.production_actions.confirm_counterparty_with_stages",
))
