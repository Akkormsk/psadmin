"""Published production knowledge capabilities using existing UI services."""
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError

from .assistant_tools import AssistantResource, AssistantTool, AssistantToolError, register_capability_exclusion, register_resource, register_tool
from .models import CounterpartyKnowledgeVersion, Proposal, WorkBoard, WorkBoardCard, WorkBoardColumn
from tender_selection.models import Tender
from .provider_knowledge import confirm_knowledge, confirm_sewing_price_list, create_counterparty_with_knowledge, remove_counterparty_knowledge
from .provider_calculators import ProviderCalculatorError, calculate_provider_binding
from .proposals import apply_batch
from .production_actions import add_route_calculation_sources, confirm_proposed_stage, confirm_route_training, deactivate_lesson, deactivate_route_knowledge, confirm_counterparty_with_stages, draft_production_counterparty, draft_production_feedback, reopen_confirmed_route, restore_requirement_to_route, revise_route_build, save_manual_training_example, select_route_catalog_product, start_route_build
from .estimate_actions import duplicate_estimate, delete_estimate, set_tender_estimate_status, set_order_estimate_status, persist_estimate, estimate_for_user, EstimateActionError
from .workboards import add_column, create_board, create_task_card, create_task_from_plan, create_tender_card, delete_card, edit_task_work, move_card
from .product_editing import save_manual_step, save_products


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
register_resource(AssistantResource(
    "tenders.workboards", "Доски",
    "Рабочие доски и их владельцы.",
    "tenders.WorkBoard", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.workboard_columns", "Колонки досок",
    "Очередь, завершение и промежуточные колонки рабочих досок.",
    "tenders.WorkBoardColumn", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.workboard_cards", "Карточки досок",
    "Карточки задач и исполнения выигранных тендеров.",
    "tenders.WorkBoardCard", ("tenders",),
))
register_resource(AssistantResource(
    "tenders.tasks", "Задачи",
    "Самостоятельные задачи с описанием, подзадачами, этапами и проверками.",
    "tenders.Task", ("tenders",),
))


WORKBOARD_TASK_ACTIONS = (
    ("edit_task", "Изменить задачу"), ("add_item", "Добавить подзадачу"),
    ("edit_item", "Изменить подзадачу"), ("move_item", "Переместить подзадачу"),
    ("delete_item", "Удалить подзадачу"), ("add_step", "Добавить этап"),
    ("move_step", "Переместить этап"), ("delete_step", "Удалить этап"),
    ("add_check", "Добавить проверку"), ("move_check", "Переместить проверку"),
    ("delete_check", "Удалить проверку"), ("toggle_check", "Отметить проверку"),
)


def _workboard_error(error):
    if isinstance(error, ValidationError):
        return " ".join(error.messages)
    return str(error) or "Операция с доской недоступна"


def _board_for_user(user, board_id):
    board = WorkBoard.objects.get(pk=board_id)
    if not user.is_superuser and board.created_by_id != user.pk:
        raise PermissionDenied("Нет доступа к этой доске")
    return board


def _card_for_user(user, card_id):
    card = WorkBoardCard.objects.select_related("board").get(pk=card_id)
    _board_for_user(user, card.board_id)
    return card


def _run_workboard(action):
    try:
        return action()
    except (ObjectDoesNotExist, PermissionDenied, ValidationError, ValueError, TypeError) as error:
        raise AssistantToolError(_workboard_error(error)) from error


def assistant_create_board(user, payload):
    board = _run_workboard(lambda: create_board(user, payload["name"]))
    return {"board_id": board.pk}


def assistant_add_column(user, payload):
    column = _run_workboard(lambda: add_column(_board_for_user(user, payload["board_id"]), payload["name"]))
    return {"column_id": column.pk, "board_id": column.board_id}


def assistant_create_task(user, payload):
    card = _run_workboard(lambda: create_task_card(
        _board_for_user(user, payload["board_id"]),
        name=payload["name"], description=payload.get("description", ""),
    ))
    return {"card_id": card.pk, "task_id": card.task_id, "board_id": card.board_id}


def assistant_add_tender_card(user, payload):
    def execute():
        if not user.is_superuser:
            raise PermissionDenied("Передавать тендеры в исполнение может только администратор")
        tender = Tender.objects.get(pk=payload["tender_id"], outcome_status=Tender.OUTCOME_WON)
        return create_tender_card(_board_for_user(user, payload["board_id"]), tender)
    card = _run_workboard(execute)
    return {"card_id": card.pk, "tender_id": card.tender_id, "board_id": card.board_id}


def assistant_move_card(user, payload):
    def execute():
        card = _card_for_user(user, payload["card_id"])
        column = WorkBoardColumn.objects.get(pk=payload["column_id"], board_id=card.board_id)
        return move_card(card, column)
    card = _run_workboard(execute)
    return {"card_id": card.pk, "column_id": card.column_id}


def assistant_edit_task(user, payload):
    data = {key: value for key, value in payload.items() if key not in {"card_id", "action"}}
    card = _run_workboard(lambda: edit_task_work(
        _card_for_user(user, payload["card_id"]), user, payload["action"], data,
    ))
    return {"card_id": card.pk, "task_id": card.task_id, "action": payload["action"]}


def assistant_import_task(user, payload):
    card = _run_workboard(lambda: create_task_from_plan(
        _board_for_user(user, payload["board_id"]), user, payload["plan"],
    ))
    return {"card_id": card.pk, "task_id": card.task_id, "board_id": card.board_id}


def assistant_delete_card(user, payload):
    card = _card_for_user(user, payload["card_id"])
    result = {"card_id": card.pk, "board_id": card.board_id, "task_id": card.task_id, "tender_id": card.tender_id}
    _run_workboard(lambda: delete_card(card, user))
    return result


register_tool(AssistantTool(
    "workboard.create", "Создать рабочую доску", "Создаёт доску с колонками очереди и завершения.",
    {"name": {"type": "string", "required": True, "label": "Название доски"}},
    False, True, False, assistant_create_board, source="tenders.workboards.create_board",
))
register_tool(AssistantTool(
    "workboard.column_add", "Добавить колонку доски", "Добавляет промежуточную колонку перед завершением.",
    {"board_id": {"type": "integer", "required": True, "reference": {"model": "tenders.WorkBoard", "label": "name"}}, "name": {"type": "string", "required": True, "label": "Название колонки"}},
    False, True, False, assistant_add_column, source="tenders.workboards.add_column",
))
register_tool(AssistantTool(
    "workboard.task_create", "Создать задачу на доске", "Создаёт простую задачу в очереди выбранной доски.",
    {"board_id": {"type": "integer", "required": True, "reference": {"model": "tenders.WorkBoard", "label": "name"}}, "name": {"type": "string", "required": True, "label": "Название задачи"}, "description": "string?"},
    False, True, False, assistant_create_task, source="tenders.workboards.create_task_card",
))
register_tool(AssistantTool(
    "workboard.tender_add", "Передать выигранный тендер на доску", "Создаёт одну карточку исполнения выигранного тендера.",
    {"board_id": {"type": "integer", "required": True, "reference": {"model": "tenders.WorkBoard", "label": "name"}}, "tender_id": {"type": "integer", "required": True, "reference": {"model": "tender_selection.Tender", "label": ["purchase_number", "title"]}}},
    False, True, True, assistant_add_tender_card, source="tenders.workboards.create_tender_card",
))
register_tool(AssistantTool(
    "workboard.card_move", "Переместить карточку", "Перемещает карточку в другую колонку той же доски.",
    {"card_id": {"type": "integer", "required": True, "reference": {"model": "tenders.WorkBoardCard", "label": ["task", "tender"]}}, "column_id": {"type": "integer", "required": True, "reference": {"model": "tenders.WorkBoardColumn", "label": "name"}}},
    False, True, False, assistant_move_card, source="tenders.workboards.move_card",
))
register_tool(AssistantTool(
    "workboard.task_edit", "Изменить состав и выполнение задачи", "Изменяет задачу, подзадачи, этапы или проверки через общий исполнитель карточки.",
    {"card_id": {"type": "integer", "required": True, "reference": {"model": "tenders.WorkBoardCard", "label": ["task", "tender"]}}, "action": {"type": "string", "required": True, "label": "Действие", "choices": WORKBOARD_TASK_ACTIONS}, "name": "string?", "description": "string?", "item_id": "integer?", "step_id": "integer?", "checkpoint_id": "integer?", "direction": "string?", "criterion": "string?", "required": "boolean?", "completed": "boolean?"},
    False, True, False, assistant_edit_task, source="tenders.workboards.edit_task_work",
))
register_tool(AssistantTool(
    "workboard.task_import", "Создать задачу по готовому плану", "Идемпотентно создаёт задачу с подзадачами, этапами и обязательными проверками.",
    {"board_id": {"type": "integer", "required": True, "reference": {"model": "tenders.WorkBoard", "label": "name"}}, "plan": {"type": "object", "required": True, "label": "План задачи"}},
    False, True, False, assistant_import_task, source="tenders.workboards.create_task_from_plan",
))
register_tool(AssistantTool(
    "workboard.card_delete", "Удалить карточку доски", "Удаляет задачу целиком или снимает карточку исполнения тендера с доски.",
    {"card_id": {"type": "integer", "required": True, "reference": {"model": "tenders.WorkBoardCard", "label": ["task", "tender"]}}},
    False, True, False, assistant_delete_card, source="tenders.workboards.delete_card",
))


def assistant_save_products(user, payload):
    try:
        return save_products(user, payload["tender_id"], payload["state"], route_only=True)
    except (ObjectDoesNotExist, PermissionDenied, ValidationError, ValueError, TypeError) as error:
        raise AssistantToolError(_workboard_error(error)) from error


def assistant_save_manual_step(user, payload):
    try:
        result = save_manual_step(
            user, payload["tender_id"], payload["source_id"], payload["plan_id"],
            payload["step_id"], payload["mode"], payload.get("amount"),
        )
        return {"saved": True, "result": result}
    except (ObjectDoesNotExist, PermissionDenied, ValidationError, ValueError, TypeError) as error:
        raise AssistantToolError(_workboard_error(error)) from error


register_tool(AssistantTool(
    "tender.products_save", "Сохранить рабочий состав товаров",
    "Сохраняет проверенный состав, количество, компоненты и маршрутные основания по той же ревизии, что редактор карточки тендера.",
    {"tender_id": {"type": "integer", "required": True, "reference": {"model": "tender_selection.Tender", "label": ["purchase_number", "title"]}}, "state": {"type": "object", "required": True, "label": "Проверенное состояние редактора товаров"}},
    False, True, True, assistant_save_products, source="tenders.product_editing.save_products",
))
register_tool(AssistantTool(
    "tender.manual_step_price_save", "Сохранить ручную цену этапа",
    "Сохраняет или очищает цену одного этапа согласованного маршрута и пересчитывает стоимость позиции.",
    {"tender_id": {"type": "integer", "required": True, "reference": {"model": "tender_selection.Tender", "label": ["purchase_number", "title"]}}, "source_id": "integer", "plan_id": "integer", "step_id": "integer", "mode": {"type": "string", "required": True, "label": "Способ ввода", "choices": (("unit", "За единицу"), ("total", "За тираж"))}, "amount": "number?"},
    False, True, True, assistant_save_manual_step, source="tenders.product_editing.save_manual_step",
))


register_capability_exclusion("tenders.production_actions.activate_cascade_lab_settings", "Internal cascade laboratory settings are not a user business operation.")
register_capability_exclusion("tenders.cascade_lab.execute_cascade_steps", "Internal paid cascade laboratory runs remain outside assistant tools.")
register_capability_exclusion("tenders.gateway_budget.available_models", "Model inventory is an internal dependency of the cascade laboratory.")
register_capability_exclusion("tenders.production_actions.save_cascade_lab_preset", "Internal cascade laboratory presets are not a user business operation.")
register_capability_exclusion("tenders.services.recognize_tender_items", "Legacy binary upload analysis is replaced by the saved tender document workflow.")
register_capability_exclusion("tenders.services.inspect_tender_document", "Legacy binary upload inspection is replaced by the saved tender document workflow.")
register_capability_exclusion("tenders.services.detect_tender_document_type", "Legacy binary upload detection is an internal step of document analysis.")
register_capability_exclusion("tenders.services.analyze_tender_requirements", "Legacy binary requirement analysis is replaced by the saved tender document workflow.")


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
    "Создаёт или обновляет расчёт, вычисляет экономику и автоматически сохраняет все позиции в переданном порядке и свод по тем же правилам, что и страница сайта. Принадлежность позиции к группе задаётся group_key и group_name; одинаковый ключ объединяет позиции только внутри этого расчёта. Общая НМЦК исходного набора учитывается один раз при неизменном полном составе и количествах; себестоимость складывается из товаров, НМЦК между ними не распределяется. При изменении состава или количества исходная цена требует проверки. Прибыль и ROI группы не включают общую доставку расчёта; в общем итоге она учитывается один раз.",
    {'estimate_id': {'type': 'integer', 'required': False, 'label': 'Изменяемый расчёт', 'reference': {'model': [{'when': {'pipeline': True}, 'model': 'tenders.TenderEstimate'}, {'when': {'pipeline': False}, 'model': 'tenders.OrderEstimate'}, {'when': {}, 'model': 'tenders.TenderEstimate'}], 'label': ['tender_number', 'order_number', 'name']}}, 'pipeline': {'type': 'boolean', 'required': False, 'label': 'Расчёт относится к тендеру'}, 'tender_number': 'string', 'name': 'string', 'lines': {'type': 'array', 'required': True, 'label': 'Позиции расчёта'}, 'reduction_percent': 'number?', 'russia_delivery': 'number?', 'result_notes': 'string?', 'document_analysis': 'object?', 'owner_id': {'type': 'integer', 'required': False, 'label': 'Ответственный', 'reference': {'model': 'auth.User', 'label': ['username']}}},
    False, True, True, save_business_estimate,
    ui={'model': 'tenders.TenderEstimate'}, source="tenders.estimate_actions.persist_estimate",
))
