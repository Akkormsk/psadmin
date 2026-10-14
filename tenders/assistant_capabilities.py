"""Published production knowledge capabilities using existing UI services."""
from .assistant_tools import AssistantTool, AssistantToolError, register_tool
from .models import CounterpartyKnowledgeVersion, Proposal
from .provider_knowledge import confirm_knowledge, confirm_sewing_price_list, create_counterparty_with_knowledge, remove_counterparty_knowledge
from .provider_calculators import ProviderCalculatorError, calculate_provider_binding
from .proposals import apply_batch
from .production_actions import activate_cascade_lab_settings, deactivate_lesson, deactivate_route_knowledge, confirm_counterparty_with_stages, draft_production_feedback, save_cascade_lab_preset
from .estimate_actions import duplicate_estimate, delete_estimate, set_tender_estimate_status, set_order_estimate_status, persist_estimate, estimate_for_user, EstimateActionError


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


def create_counterparty(user, payload):
    try:
        return create_counterparty_with_knowledge(
            user, name=payload["name"], stage_ids=payload["stage_ids"], source_text=payload.get("source_text", ""),
        )
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


def calculate_counterparty(user, payload):
    try:
        return calculate_provider_binding(user, payload["binding_id"], payload["parameters"])
    except ProviderCalculatorError as exc:
        raise AssistantToolError(str(exc)) from exc


def remove_counterparty_knowledge_action(user, payload):
    try:
        return remove_counterparty_knowledge(user, counterparty_id=payload["counterparty_id"], version_id=payload["knowledge_version_id"])
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


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
    "provider.knowledge_remove", "Удалить черновик или скрыть устаревшие знания контрагента", "Использует действие карточки контрагента; действующий подтверждённый прайс удалить нельзя.",
    {"counterparty_id": "integer", "knowledge_version_id": "integer"}, False, True, True,
    remove_counterparty_knowledge_action, source="tenders.provider_knowledge.remove_counterparty_knowledge",
))
register_tool(AssistantTool(
    "provider.calculator_run", "Рассчитать у контрагента", "Вызывает выбранный действующий калькулятор контрагента тем же исполнителем, что страница его калькулятора.",
    {"binding_id": "integer", "parameters": "object"}, True, False, True,
    calculate_counterparty, source="tenders.provider_calculators.calculate_provider_binding",
))

register_tool(AssistantTool(
    "provider.create", "Создать контрагента и подготовить знания", "Использует существующую форму контрагента: создаёт карточку, привязывает этапы и готовит неподтверждённый черновик знаний для проверки.",
    {"name": "string", "stage_ids": "integer[]", "source_text": "string?"}, False, True, True,
    create_counterparty, source="tenders.provider_knowledge.create_counterparty_with_knowledge",
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
    pipeline = payload.get("pipeline")
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
    {"estimate_id": "integer", "pipeline": "boolean"}, False, True, True,
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

def prepare_production_changes(user, payload):
    from .services import TenderAIError
    try:
        return draft_production_feedback(user, payload["text"])
    except (ValueError, TenderAIError) as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool(
    "production.feedback_draft", "Подготовить правки базы производства по описанию",
    "Разбирает свободное описание исправлений на независимые предложения для проверки. Записывает только ожидающие подтверждения предложения, не изменяет глобальные правила.",
    {"text": "string"}, False, True, True, prepare_production_changes,
    source="tenders.production_actions.draft_production_feedback",
))

register_tool(AssistantTool(
    "cascade.preset_save", "Сохранить набор настроек лаборатории Cascade",
    "Сохраняет именованный набор настроек тем же исполнителем, что кнопка лаборатории Cascade.",
    {"name": "string", "settings": "object"}, False, True, True,
    lambda user, payload: save_cascade_lab_preset(user, payload["name"], payload["settings"]),
    source="tenders.production_actions.save_cascade_lab_preset",
))
register_tool(AssistantTool(
    "cascade.settings_activate", "Применить настройки Cascade",
    "Создаёт и активирует версию настроек Cascade тем же исполнителем, что кнопка лаборатории.",
    {"name": "string?", "settings": "object"}, False, True, True,
    lambda user, payload: activate_cascade_lab_settings(user, payload.get("name"), payload["settings"]),
    source="tenders.production_actions.activate_cascade_lab_settings",
))

def save_business_estimate(user, payload):
    import json
    from .models import TenderSettings

    if not isinstance(payload.get("lines"), list):
        raise AssistantToolError("Нужен список позиций расчёта")
    pipeline = payload.get("pipeline", True)
    if not isinstance(pipeline, bool):
        raise AssistantToolError("Тип расчёта должен быть задан однозначно")
    estimate = None
    if payload.get("estimate_id") not in (None, ""):
        try:
            estimate = estimate_for_user(user, payload["estimate_id"], pipeline)
        except EstimateActionError as exc:
            raise AssistantToolError(str(exc)) from exc
    data = {
        "tender_number": str(payload["tender_number"]),
        "name": str(payload["name"]),
        "lines_json": json.dumps(payload["lines"], ensure_ascii=False),
        "document_analysis_json": json.dumps(payload.get("document_analysis") or {}, ensure_ascii=False),
        "reduction_percent": payload.get("reduction_percent"),
        "russia_delivery": payload.get("russia_delivery"),
        "result_notes": str(payload.get("result_notes", "")),
    }
    if user.is_superuser and payload.get("owner_id"):
        data["owner_id"] = payload["owner_id"]
    settings, _ = TenderSettings.objects.get_or_create(pk=1)
    saved, error, meta = persist_estimate(user, data, estimate, settings, pipeline=pipeline)
    if error:
        raise AssistantToolError(error)
    return {"estimate_id": saved.pk, "pipeline": pipeline, "incomplete": meta["incomplete"]}


register_tool(AssistantTool(
    "estimate.save", "Создать или изменить расчёт с автоматическим пересчётом",
    "Создаёт или обновляет расчёт, вычисляет экономику и автоматически сохраняет все позиции и свод по тем же правилам, что и страница сайта.",
    {"estimate_id": "integer?", "pipeline": "boolean?", "tender_number": "string",
     "name": "string", "lines": "array", "reduction_percent": "number?",
     "russia_delivery": "number?", "result_notes": "string?",
     "document_analysis": "object?", "owner_id": "integer?"},
    False, True, True, save_business_estimate,
    source="tenders.estimate_actions.persist_estimate",
))
