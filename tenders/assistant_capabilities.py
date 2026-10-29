"""Published production knowledge capabilities using existing UI services."""
from .assistant_tools import AssistantResource, AssistantTool, AssistantToolError, register_resource, register_tool
from .models import CounterpartyKnowledgeVersion, Proposal
from tender_selection.models import Tender
from .provider_knowledge import confirm_knowledge, confirm_sewing_price_list, create_counterparty_with_knowledge, remove_counterparty_knowledge
from .provider_calculators import ProviderCalculatorError, calculate_provider_binding
from .proposals import apply_batch
from .production_actions import add_route_calculation_sources, confirm_proposed_stage, confirm_route_training, deactivate_lesson, deactivate_route_knowledge, confirm_counterparty_with_stages, draft_production_counterparty, draft_production_feedback, reopen_confirmed_route, restore_requirement_to_route, revise_route_build, save_manual_training_example, select_route_catalog_product, start_route_build
from .estimate_actions import duplicate_estimate, delete_estimate, set_tender_estimate_status, set_order_estimate_status, persist_estimate, estimate_for_user, EstimateActionError


register_resource(AssistantResource(
    "tenders.orders", "Заказы",
    "Заказы вне тендерного пайплайна и их текущие стадии.",
    "tenders.Order", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.order_estimates", "Расчёты заказов",
    "Сохранённые расчёты цен и экономики по заказам.",
    "tenders.OrderEstimate", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.counterparties", "Контрагенты",
    "Контрагенты производственной базы и их активность.",
    "tenders.Counterparty", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.production_stages", "Этапы производства",
    "Настроенные этапы производственного маршрута.",
    "tenders.ProcessDefinition", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.tender_estimates", "Расчёты тендеров",
    "Сохранённые расчёты цены и экономики по тендерам. Статус расчёта как на сайте — display_status; tender.status относится к тендеру.",
    "tenders.TenderEstimate", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.tender_lines", "Позиции расчётов тендеров",
    "Товары и работы внутри расчётов тендеров.",
    "tenders.TenderLine", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.order_lines", "Позиции расчётов заказов",
    "Товары и работы внутри расчётов заказов.",
    "tenders.OrderLine", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.settings", "Настройки тендерных расчётов",
    "Общие настройки расчётов тендеров.",
    "tenders.TenderSettings", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.production_types", "Типы производства",
    "Настроенные типы производственных маршрутов.",
    "tenders.ProductionType", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.lessons", "Правила производства",
    "Подтверждённые правила и знания для производственных расчётов.",
    "tenders.Lesson", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.catalog_suppliers", "Поставщики каталогов",
    "Поставщики, из чьих каталогов доступны товары и цены.",
    "tenders.CatalogSupplier", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.catalog_categories", "Категории каталогов",
    "Категории товаров поставщиков.",
    "tenders.CatalogCategory", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.catalog_products", "Товары каталогов",
    "Товары, цены и доступность из подключённых каталогов поставщиков.",
    "tenders.CatalogProduct", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.stage_counterparties", "Связи этапов и контрагентов",
    "Доступные контрагенты, приоритет и способ получения цены для этапов производства.",
    "tenders.StageCounterpartyLink", ("tenders",),
))


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
    {'version_id': {'type': 'integer', 'required': True, 'label': 'Версия знаний контрагента', 'reference': {'model': 'tenders.CounterpartyKnowledgeVersion', 'label': ['counterparty', 'version_number']}}, 'note': {'type': 'string', 'required': False, 'label': 'Комментарий к подтверждению'}}, False, True, True,
    confirm_provider_knowledge, source="tenders.provider_knowledge.confirm_knowledge",
))
register_tool(AssistantTool(
    "provider.knowledge_remove", "Удалить черновик или скрыть устаревшие знания контрагента", "Использует действие карточки контрагента; действующий подтверждённый прайс удалить нельзя.",
    {'counterparty_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.Counterparty', 'label': ['name']}}, 'knowledge_version_id': {'type': 'integer', 'required': True, 'label': 'Версия знаний', 'reference': {'model': 'tenders.CounterpartyKnowledgeVersion', 'label': ['counterparty', 'version_number']}}}, False, True, True,
    remove_counterparty_knowledge_action, source="tenders.provider_knowledge.remove_counterparty_knowledge",
))
register_tool(AssistantTool(
    "provider.calculator_run", "Рассчитать у контрагента", "Вызывает выбранный действующий калькулятор контрагента тем же исполнителем, что страница его калькулятора.",
    {"binding_id": "integer", "parameters": "object"}, True, False, True,
    calculate_counterparty, source="tenders.provider_calculators.calculate_provider_binding",
))

register_tool(AssistantTool(
    "provider.create", "Создать контрагента и подготовить знания", "Использует существующую форму контрагента: создаёт карточку, привязывает этапы и готовит неподтверждённый черновик знаний для проверки.",
    {'name': 'string', 'stage_ids': {'type': 'integer[]', 'required': True, 'reference': {'model': 'tenders.ProcessDefinition', 'label': 'name'}}, 'source_text': {'type': 'string', 'required': False, 'label': 'Исходные сведения'}}, False, True, True,
    create_counterparty, ui={"model": "tenders.Counterparty"}, source="tenders.provider_knowledge.create_counterparty_with_knowledge",
))

register_tool(AssistantTool(
    "production.feedback_decide", "Применить исправления производства",
    "Принять выбранные предложения из одной карточки обучения, остальные отклонить по штатным правилам.",
    {'batch_id': {'type': 'string', 'required': True, 'label': 'Набор предложений'}, 'accepted_ids': {'type': 'integer[]', 'required': True, 'label': 'Принимаемые изменения', 'reference': {'model': 'tenders.Proposal', 'label': ['summary']}}}, False, True, True,
    decide_feedback_proposals, source="tenders.proposals.apply_batch",
))

register_tool(AssistantTool(
    "provider.sewing_price_confirm", "Подтвердить прайс пошива и активировать калькулятор",
    "После проверки распознанного XLS/XLSX подтверждает валюту и смысл формулы, фиксирует версию прайса и активирует существующий калькулятор.",
    {'version_id': {'type': 'integer', 'required': True, 'label': 'Версия прайса', 'reference': {'model': 'tenders.CounterpartyKnowledgeVersion', 'label': ['counterparty', 'version_number']}}, 'currency': {'type': 'string', 'required': True, 'label': 'Валюта прайса'}, 'formula_confirmed': {'type': 'boolean', 'required': True, 'label': 'Формула проверена и подтверждена'}}, False, True, True,
    confirm_sewing_price, source="tenders.provider_knowledge.confirm_sewing_price_list",
))

def deactivate_training_lesson(user, payload):
    return {"dropped": deactivate_lesson(payload["lesson_id"])}


register_tool(AssistantTool(
    "production.lesson_deactivate", "Отключить ранее выученное правило",
    "Скрыть выбранное правило из следующих производственных расчётов, не удаляя историю обучения.",
    {'lesson_id': {'type': 'integer', 'required': True, 'label': 'Выученное правило', 'reference': {'model': 'tenders.Lesson', 'label': ['summary', 'admin_text']}}}, False, True, True,
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
    {'estimate_id': {'type': 'integer', 'required': True, 'label': 'Выбранный расчёт', 'reference': {'model': [{'when': {'pipeline': True}, 'model': 'tenders.TenderEstimate'}, {'when': {'pipeline': False}, 'model': 'tenders.OrderEstimate'}], 'label': ['tender_number', 'order_number', 'name']}}, 'pipeline': {'type': 'boolean', 'required': True, 'label': 'Расчёт относится к тендеру'}}, False, True, True,
    lambda user, payload: manage_saved_estimate(user, payload),
    source="tenders.estimate_actions.duplicate_estimate",
))
register_tool(AssistantTool(
    "estimate.delete", "Удалить сохранённый тендерный или заказной расчёт",
    "Удаляет выбранный расчёт через штатный механизм с проверкой владельца; операция необратима.",
    {'estimate_id': {'type': 'integer', 'required': True, 'label': 'Выбранный расчёт', 'reference': {'model': [{'when': {'pipeline': True}, 'model': 'tenders.TenderEstimate'}, {'when': {'pipeline': False}, 'model': 'tenders.OrderEstimate'}], 'label': ['tender_number', 'order_number', 'name']}}, 'pipeline': {'type': 'boolean', 'required': False, 'label': 'Расчёт относится к тендеру'}}, False, True, True,
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
    {'estimate_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.TenderEstimate', 'label': ['tender_number', 'name']}}, 'status': {'type': 'string', 'required': True, 'label': 'Стадия тендерного расчёта', 'choices': Tender.OUTCOME_STATUS_CHOICES}}, False, True, True,
    lambda user, payload: set_estimate_outcome(user, payload, pipeline=True),
    source="tenders.estimate_actions.set_tender_estimate_status",
))
register_tool(AssistantTool(
    "estimate.order_status", "Изменить стадию отдельного заказа",
    "Меняет статус заказа и при необходимости создаёт связанную карточку заказа так же, как существующая страница.",
    {'estimate_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.OrderEstimate', 'label': ['order_number', 'name']}}, 'status': 'string'}, False, True, True,
    lambda user, payload: set_estimate_outcome(user, payload, pipeline=False),
    ui={'model': 'tenders.Order'}, source="tenders.estimate_actions.set_order_estimate_status",
))

def deactivate_route_experience(user, payload):
    try:
        return {"dropped": deactivate_route_knowledge(payload)}
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool(
    "production.route_knowledge_deactivate", "Отключить неверный производственный маршрут",
    "Отключает выбранный подтверждённый маршрут и связанные с ним выученные правила по тому же алгоритму, что и интерфейс.",
    {'lesson_id': {'type': 'integer', 'required': False, 'label': 'Правило маршрута', 'reference': {'model': 'tenders.Lesson', 'label': ['summary', 'admin_text']}}, 'example_id': {'type': 'integer', 'required': False, 'label': 'Пример маршрута', 'reference': {'model': 'tenders.ProductionTrainingExample', 'label': ['position_name']}}}, False, True, True,
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
    {'name': 'string', 'notes': 'string?', 'stage_ids': {'type': 'integer[]', 'required': True, 'reference': {'model': 'tenders.ProcessDefinition', 'label': 'name'}}, 'source_id': {'type': 'integer', 'required': False, 'label': 'Источник сведений', 'reference': {'model': 'tenders.TenderKnowledgeSource', 'label': ['title', 'supplier_name']}}},
    False, True, True, confirm_production_counterparty,
    ui={"model": "tenders.Counterparty"}, source="tenders.production_actions.confirm_counterparty_with_stages",
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
    {'text': {'type': 'string', 'required': True, 'label': 'Исправления производственной базы'}}, False, True, True, prepare_production_changes,
    source="tenders.production_actions.draft_production_feedback",
))

register_tool(AssistantTool(
    "production.requirement_skip_restore", "Вернуть требование ТЗ в подбор",
    "Отменяет сохранённое исключение строки ТЗ тем же исполнителем, что кнопка маршрута.",
    {'label': {'type': 'string', 'required': True, 'label': 'Восстанавливаемое требование'}}, False, True, True,
    lambda user, payload: restore_requirement_to_route(user, payload["label"]),
    source="tenders.production_actions.restore_requirement_to_route",
))
register_tool(AssistantTool(
    "production.stage_proposal_confirm", "Добавить предложенный этап производства",
    "Подтверждает один предложенный этап маршрута; затем маршрут нужно пересчитать, как и после нажатия кнопки на сайте.",
    {'session_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.ProductionTrainingSession', 'label': ['position_name']}, 'label': 'Расчёт производственного маршрута'}, 'proposal_id': {'type': 'string', 'required': True, 'label': 'Предлагаемый этап'}}, False, True, True,
    lambda user, payload: confirm_proposed_stage(user, payload["session_id"], payload["proposal_id"]),
    source="tenders.production_actions.confirm_proposed_stage",
))
register_tool(AssistantTool(
    "production.route_reopen", "Вернуть подтверждённый маршрут на доработку",
    "Создаёт редактируемую версию подтверждённого маршрута и сохраняет историю прежней версии, как кнопка экрана маршрута.",
    {'session_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.ProductionTrainingSession', 'label': ['position_name']}, 'label': 'Расчёт производственного маршрута'}}, False, True, True,
    lambda user, payload: reopen_confirmed_route(user, payload["session_id"]),
    source="tenders.production_actions.reopen_confirmed_route",
))
register_tool(AssistantTool(
    "production.catalog_product_select", "Выбрать товар поставщика для маршрута",
    "Фиксирует явно выбранный товар в текущем маршруте и сохраняет решение для последующего расчёта.",
    {'session_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.ProductionTrainingSession', 'label': ['position_name']}, 'label': 'Расчёт производственного маршрута'}, 'line': {'type': 'object', 'required': True, 'label': 'Позиция расчёта'}, 'product_id': {'type': 'string', 'required': True, 'label': 'Артикул выбранного товара'}, 'step_id': {'type': 'string', 'required': False, 'label': 'Шаг маршрута'}}, False, True, True,
    lambda user, payload: select_route_catalog_product(user, payload),
    source="tenders.production_actions.select_route_catalog_product",
))
register_tool(AssistantTool(
    "production.route_confirm", "Подтвердить маршрут и сохранить обучение",
    "Подтверждает готовый маршрут, сохраняет его знания и отмеченные исключения ТЗ тем же действием, что кнопка расчёта.",
    {'session_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.ProductionTrainingSession', 'label': ['position_name']}, 'label': 'Расчёт производственного маршрута'}}, False, True, True,
    lambda user, payload: confirm_route_training(user, payload["session_id"]),
    source="tenders.production_actions.confirm_route_training",
))
register_tool(AssistantTool(
    "provider.production_draft", "Подготовить карточку производственного контрагента",
    "Разбирает текст или ссылку на данные контрагента в проверяемый черновик; карточка контрагента создаётся отдельным подтверждаемым действием.",
    {'text': {'type': 'string', 'required': False, 'label': 'Сведения о контрагенте'}, 'url': {'type': 'string', 'required': False, 'label': 'Источник сведений'}}, False, True, True,
    lambda user, payload: draft_production_counterparty(user, payload.get("text", ""), payload.get("url", "")),
    source="tenders.production_actions.draft_production_counterparty",
))
register_tool(AssistantTool(
    "production.calculation_sources_add", "Добавить источники в расчёт маршрута",
    "Добавляет один или несколько проверяемых источников и пересчитывает текущий маршрут тем же исполнителем, что экран расчёта.",
    {'session_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.ProductionTrainingSession', 'label': ['position_name']}, 'label': 'Расчёт производственного маршрута'}, 'line': {'type': 'object', 'required': True, 'label': 'Позиция расчёта'}, 'sources': {'type': 'array', 'required': False, 'label': 'Источники расчёта'}, 'source_text': {'type': 'string', 'required': False, 'label': 'Текст источника'}, 'source_url': {'type': 'string', 'required': False, 'label': 'Ссылка на источник'}, 'feedback': {'type': 'string', 'required': False, 'label': 'Уточнения к источнику'}}, False, True, True,
    lambda user, payload: add_route_calculation_sources(user, payload),
    source="tenders.production_actions.add_route_calculation_sources",
))
def start_production_route(user, payload):
    from .views import _ASSISTANT_STALE_AFTER, _record_stage, _submit_assistant_job
    return start_route_build(
        user, payload["line"], payload.get("start_catalog") is True,
        submit_job=_submit_assistant_job, record_stage=_record_stage, stale_after=_ASSISTANT_STALE_AFTER,
    )


register_tool(AssistantTool(
    "production.route_build", "Построить маршрут производства",
    "Запускает существующее фоновое построение маршрута для позиции; возвращает идентификатор сессии для продолжения диалога.",
    {'line': {'type': 'object', 'required': True, 'label': 'Позиция расчёта'}, 'start_catalog': {'type': 'boolean', 'required': False, 'label': 'Начать поиск в каталоге'}}, False, True, True,
    start_production_route, source="tenders.production_actions.start_route_build",
))

def revise_production_route(user, payload):
    from .views import _ASSISTANT_STALE_AFTER, _record_stage, _submit_assistant_job
    return revise_route_build(
        user, payload, submit_job=_submit_assistant_job,
        record_stage=_record_stage, stale_after=_ASSISTANT_STALE_AFTER,
    )


register_tool(AssistantTool(
    "production.route_revise", "Уточнить и пересчитать маршрут производства",
    "Продолжает тот же диалог маршрута: учитывает ответы, подтверждённые требования или замечание и запускает штатный пересчёт.",
    {'session_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.ProductionTrainingSession', 'label': ['position_name']}, 'label': 'Расчёт производственного маршрута'}, 'line': {'type': 'object', 'required': True, 'label': 'Позиция расчёта'}, 'feedback': {'type': 'string', 'required': False, 'label': 'Уточнение маршрута'}, 'question_answers': {'type': 'object', 'required': False, 'label': 'Ответы на вопросы'}, 'accepted_requirement_proposals': {'type': 'array', 'required': False, 'label': 'Подтверждённые требования'}, 'instructions': {'type': 'array', 'required': False, 'label': 'Указания для пересчёта'}, 'clear_ranking': {'type': 'boolean', 'required': False, 'label': 'Сбросить подбор исполнителей'}, 'refresh': {'type': 'boolean', 'required': False, 'label': 'Пересчитать заново'}, 'scope': {'type': 'string', 'required': False, 'label': 'Область пересчёта', 'choices': (('route', 'Маршрут'), ('requirements', 'Требования'))}, 'step_id': {'type': 'string', 'required': False, 'label': 'Шаг маршрута'}, 'learn_for_similar': {'type': 'boolean', 'required': False, 'label': 'Учитывать для похожих позиций'}},
    False, True, True, revise_production_route,
    source="tenders.production_actions.revise_route_build",
))
register_tool(AssistantTool(
    "production.training_example_save", "Сохранить ручной пример производственного маршрута",
    "Сохраняет введённый на экране пример, создаёт недостающие этапы и заменяет прежний активный пример этой позиции.",
    {'production_type': {'type': 'string', 'required': True, 'label': 'Код типа производства'}, 'line': {'type': 'object', 'required': True, 'label': 'Позиция расчёта'}, 'features': {'type': 'array', 'required': False, 'label': 'Существенные признаки'}, 'routes': {'type': 'array', 'required': True, 'label': 'Производственные маршруты'}, 'note': {'type': 'string', 'required': False, 'label': 'Комментарий'}},
    False, True, True, lambda user, payload: save_manual_training_example(user, payload),
    source="tenders.production_actions.save_manual_training_example",
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
    "Создаёт или обновляет расчёт, вычисляет экономику и автоматически сохраняет все позиции в переданном порядке и свод по тем же правилам, что и страница сайта.",
    {'estimate_id': {'type': 'integer', 'required': False, 'label': 'Изменяемый расчёт', 'reference': {'model': [{'when': {'pipeline': True}, 'model': 'tenders.TenderEstimate'}, {'when': {'pipeline': False}, 'model': 'tenders.OrderEstimate'}, {'when': {}, 'model': 'tenders.TenderEstimate'}], 'label': ['tender_number', 'order_number', 'name']}}, 'pipeline': {'type': 'boolean', 'required': False, 'label': 'Расчёт относится к тендеру'}, 'tender_number': 'string', 'name': 'string', 'lines': {'type': 'array', 'required': True, 'label': 'Позиции расчёта'}, 'reduction_percent': 'number?', 'russia_delivery': 'number?', 'result_notes': 'string?', 'document_analysis': 'object?', 'owner_id': {'type': 'integer', 'required': False, 'label': 'Ответственный', 'reference': {'model': 'auth.User', 'label': ['username']}}},
    False, True, True, save_business_estimate,
    ui={'model': 'tenders.TenderEstimate'}, source="tenders.estimate_actions.persist_estimate",
))
