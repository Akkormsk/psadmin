"""Published references to tender-selection executors shared with the site."""
from tenders.assistant_tools import AssistantResource, AssistantTool, AssistantToolError, register_resource, register_tool

from .models import Tender

from .services import (
    RISK_LEVEL_CHOICES,
    answer_tender_owner_question, change_forecast_inclusion, create_tender_estimate, hide_tender,
    decide_document_proposals, list_tender_documents, preview_tender_document, record_tender_outcome,
    restore_hidden_tender, save_evaluation_settings, save_selection_settings, save_tender_bid,
    update_tender_review, refresh_document_analysis, decide_all_document_proposals, import_tender_by_number, source_route_for, tender_refresh_status_for,
)


register_resource(AssistantResource(
    "tender_selection.tenders", "Тендеры",
    "Тендеры из ЕИС; для текущего списка «Входящие» используй доступное бизнес-представление «incoming».",
    "tender_selection.Tender", ("tender_selection",),
))
register_resource(AssistantResource(
    "tender_selection.customers", "Заказчики",
    "Карточки заказчиков, связанные с тендерами.",
    "tender_selection.Organization", ("tender_selection",),
))
register_resource(AssistantResource(
    "tender_selection.contract_statistics", "Статистика контрактов",
    "Исторические завершённые закупки и их фактические цены для анализа рынка.",
    "tender_selection.ContractStat", ("tender_selection",),
))
register_resource(AssistantResource(
    "tender_selection.filter_settings", "Настройки отбора тендеров",
    "Действующие плюс- и минус-слова, ценовые и временные ограничения отбора.",
    "tender_selection.FilterSettings", ("tender_selection",),
))


def _save_discovery_settings(_user, payload):
    try:
        return save_selection_settings(payload)
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


def _answer_owner_question(user, payload):
    try:
        return answer_tender_owner_question(
            user, payload["tender_id"], payload["interaction_id"], payload["answer"],
        )
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


def _save_evaluation_settings(_user, payload):
    try:
        return save_evaluation_settings(payload)
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


def _tender_action(call, user, payload, *names):
    try:
        return call(user, *(payload[name] for name in names))
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


def _hide_tender(user, payload):
    try:
        return hide_tender(user, payload["tender_id"], reason=payload.get("reason"))
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


def _start_evaluation(user, payload):
    from .models import Tender

    return _tender_action(update_tender_review, user, {
        "tender_id": payload["tender_id"], "review": Tender.INTERESTING,
    }, "tender_id", "review")


def _preview_tender_document(user, payload):
    try:
        return preview_tender_document(
            user, payload["tender_id"], payload["document_index"],
            refresh=payload.get("refresh", False),
        )
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


def _save_bid(user, payload):
    try:
        return save_tender_bid(user, payload["estimate_id"], bid_number=payload.get("bid_number", ""), bid_price=payload.get("bid_price"))
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


def _record_outcome(user, payload):
    try:
        return record_tender_outcome(
            user, payload["estimate_id"], status=payload.get("status"),
            actual_reduction_percent=payload.get("actual_reduction_percent"),
        )
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool(
    "tender.discovery_settings", "Настроить поиск и отбор закупок",
    "Изменяет действующие правила поиска тем же общим исполнителем, что и настройки сайта.",
    {'include_words': 'string?', 'exclude_words': 'string?', 'profile_triage_enabled': {'type': 'boolean', 'required': False, 'label': 'Помечать сомнительные тендеры'}, 'min_price': 'number?', 'window_days': 'integer?', 'okpd2_codes': 'string[]?', 'regions': 'string[]?', 'laws': {'type': 'string[]', 'required': False, 'label': 'Законы о закупках', 'choices': Tender.LAW_CHOICES}},
    False, True, True, _save_discovery_settings,
    ui={'model': 'tender_selection.FilterSettings'}, source="tender_selection.services.save_selection_settings",
))
register_tool(AssistantTool(
    "tender.outcome_record", "Получить или сохранить результат торгов", "Проверяет протокол/контракт либо сохраняет явно выбранный итог торгов тем же общим исполнителем, что карточка тендера.",
    {'estimate_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.TenderEstimate', 'label': ['tender_number', 'name']}}, 'status': {'type': 'won|lost|not_participated', 'required': False, 'label': 'Результат торгов', 'choices': Tender.OUTCOME_STATUS_CHOICES}, 'actual_reduction_percent': {'type': 'number', 'required': False, 'label': 'Фактическое снижение, %'}},
    False, True, True, _record_outcome, source="tender_selection.services.record_tender_outcome",
))
register_tool(AssistantTool(
    "tender.hide", "Скрыть неподходящий тендер", "Архивирует тендер тем же исполнителем, что кнопка интерфейса, и сохраняет причину для входящего тендера.",
    {'tender_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tender_selection.Tender', 'label': ['purchase_number', 'title']}}, 'reason': {'type': 'string', 'required': False, 'label': 'Причина скрытия'}}, False, True, True, _hide_tender,
    source="tender_selection.services.hide_tender",
))
register_tool(AssistantTool(
    "tender.restore", "Вернуть скрытый тендер",
    "Возвращает скрытый тендер в работу тем же сервисом, что кнопка восстановления в архиве.",
    {'tender_id': {'type': 'integer', 'description': 'Идентификатор выбранного скрытого тендера', 'reference': {'model': 'tender_selection.Tender', 'label': ['purchase_number', 'title']}}},
    False, True, True,
    lambda user, payload: restore_hidden_tender(user, payload["tender_id"]),
    source="tender_selection.services.restore_hidden_tender",
))
register_tool(AssistantTool(
    "tender.start_evaluation", "Взять тендер в работу",
    "Точно повторяет кнопку «В работу»: переводит входящий тендер на оценку и запускает штатную фоновую оценку рисков. Не даёт произвольно менять технический статус.",
    {'tender_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tender_selection.Tender', 'label': ['purchase_number', 'title']}}}, False, True, True, _start_evaluation,
    source="tender_selection.services.update_tender_review",
))
register_tool(AssistantTool(
    "tender.documents_list", "Показать документы тендера",
    "Показывает имена и ссылки на прикреплённые к выбранному тендеру документы. Перед разбором содержания сначала получи этот список.",
    {"tender_id": {"type": "integer", "description": "Идентификатор выбранного тендера"}},
    True, False, True, lambda user, payload: list_tender_documents(user, payload["tender_id"]),
    source="tender_selection.services.list_tender_documents",
))
register_tool(AssistantTool(
    "tender.document_preview", "Разобрать документ тендера",
    "Получает документ существующего тендера из ЕИС или кэша и возвращает его проверяемый разбор тем же исполнителем, что кнопка просмотра документа.",
    {"tender_id": "integer", "document_index": {"type": "integer", "description": "Номер документа из ранее показанного списка"}, "refresh": "boolean?"}, True, False, True,
    _preview_tender_document, source="tender_selection.services.preview_tender_document",
))
register_tool(AssistantTool(
    "tender.forecast_toggle", "Включить или исключить тендер из прогноза", "Переключает существующую запись статистики тем же правилом, что архив тендеров.",
    {'tender_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tender_selection.Tender', 'label': ['purchase_number', 'title']}}}, False, True, True,
    lambda user, payload: _tender_action(change_forecast_inclusion, user, payload, "tender_id"),
    source="tender_selection.services.change_forecast_inclusion",
))
register_tool(AssistantTool(
    "tender.send_to_calculation", "Передать тендер в расчёт", "Создаёт расчёт из принятого рабочего списка товаров с дополненным ТЗ и исходными финансовыми привязками. Существующий расчёт возвращает без перезаписи.",
    {"tender_id": {"type": "integer", "required": True,
                   "reference": {"model": "tender_selection.Tender", "label": ["purchase_number", "title"]},
                   "description": "Внутренний ID тендера из READ, не номер закупки. По номеру или названию сначала найдите тендер через READ."}}, False, True, True,
    lambda user, payload: _tender_action(create_tender_estimate, user, payload, "tender_id"),
    source="tender_selection.services.create_tender_estimate",
))
register_tool(AssistantTool(
    "tender.bid_save", "Сохранить нашу ставку по тендеру", "Сохраняет номер и сумму нашей заявки тем же сервисом, по которому система сопоставляет протокол торгов.",
    {'estimate_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tenders.TenderEstimate', 'label': ['tender_number', 'name']}}, 'bid_number': 'string?', 'bid_price': 'number?'}, False, True, True, _save_bid,
    ui={'model': 'tender_selection.Tender'}, source="tender_selection.services.save_tender_bid",
))

register_tool(AssistantTool(
    "tender.evaluation_settings", "Сохранить правила оценки тендеров",
    "Сохраняет пороги риска, ROI, НДС и прогноз тем же валидатором, что страница настроек оценки.",
    {'risk_warning_days': 'integer', 'risk_critical_days': 'integer', 'roi_good_percent': {'type': 'number', 'required': True, 'label': 'Хорошая рентабельность, %'}, 'roi_thin_percent': {'type': 'number', 'required': True, 'label': 'Минимальная рентабельность, %'}, 'vat_rate': {'type': 'number', 'required': True, 'label': 'НДС, %'}, 'default_reduction_percent': {'type': 'number', 'required': True, 'label': 'Снижение по умолчанию, %'}, 'stats_target_count': 'integer', 'stats_min_samples': 'integer', 'reduction_hint_min': 'integer', 'reduction_hint_max': 'integer', 'incoming_ttl_days': 'integer', 'level_samples_required': {'type': 'low|medium|high', 'required': True, 'label': 'Риск: требуются образцы', 'choices': RISK_LEVEL_CHOICES}, 'level_samples_impossible': {'type': 'low|medium|high', 'required': True, 'label': 'Риск: невозможно предоставить образцы', 'choices': RISK_LEVEL_CHOICES}, 'level_national_confirmation': {'type': 'low|medium|high', 'required': True, 'label': 'Риск: подтверждение происхождения', 'choices': RISK_LEVEL_CHOICES}, 'level_national_blocked': {'type': 'low|medium|high', 'required': True, 'label': 'Риск: ограничения происхождения', 'choices': RISK_LEVEL_CHOICES}, 'level_delivery_requests': {'type': 'low|medium|high', 'required': True, 'label': 'Риск: поставка по заявкам', 'choices': RISK_LEVEL_CHOICES}, 'level_delivery_open_ended': {'type': 'low|medium|high', 'required': True, 'label': 'Риск: неограниченные условия поставки', 'choices': RISK_LEVEL_CHOICES}},
    False, True, True, _save_evaluation_settings,
    ui={'model': 'tender_selection.FilterSettings'}, source="tender_selection.services.save_evaluation_settings",
))

register_tool(AssistantTool(
    "tender.owner_question_answer", "Ответить на запрос уточнения по тендеру",
    "Сохраняет ответ и закрывает вопрос тем же общим исполнителем, что карточка тендера.",
    {'tender_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tender_selection.Tender', 'label': ['purchase_number', 'title']}}, 'interaction_id': {'type': 'integer', 'required': True, 'label': 'Запрос уточнения', 'reference': {'model': 'tenders.OwnerInteraction', 'label': ['question']}}, 'answer': {'type': 'string', 'required': True, 'label': 'Ответ'}},
    False, True, True, _answer_owner_question,
    source="tender_selection.services.answer_tender_owner_question",
))
register_tool(AssistantTool(
    "tender.document_proposals", "Принять или отклонить состав из документации",
    "Применяет или отклоняет найденный состав тем же исполнителем, что карточка тендера. Уточнение того же товара объединяется с исходными характеристиками; разные значения сохраняются для проверки. Для компонентов итоговое количество вычисляется из количества исходной позиции и нормы на одну единицу; исходное извещение сохраняется.",
    {'tender_id': {'type': 'integer', 'required': True, 'reference': {'model': 'tender_selection.Tender', 'label': ['purchase_number', 'title']}}, 'parent_id': {'type': 'integer', 'required': True, 'label': 'Исходная позиция', 'reference': {'model': 'tenders.TenderSourceItem', 'label': ['original_text']}}, 'decision': {'type': 'accept|reject', 'required': True, 'label': 'Решение по составу', 'choices': (('accept', 'Принять'), ('reject', 'Отклонить'))}},
    False, True, True,
    lambda user, payload: _tender_action(
        decide_document_proposals, user, payload, "tender_id", "parent_id", "decision",
    ),
    source="tender_selection.services.decide_document_proposals",
))

register_tool(AssistantTool(
    "tender.analyze", "Обновить тендер в фоне",
    "Одной фоновой задачей обновляет извещение и документы, определяет состав товаров и наборов, проверяет 1875, сверяет ТЗ, затем сохраняет маршруты по подготовленным позициям. Каждый завершённый этап отображается сразу; актуальные результаты переиспользуются. Статусы этапов и время доступны в карточке. Не принимает правки автоматически, не создаёт расчёт и не запускает калькуляторы контрагентов. Подтверждает запуск, а не завершение.",
    {"tender_id": {"type": "integer", "required": True, "reference": {"model": "tender_selection.Tender", "label": ["purchase_number", "title"]}}},
    False, True, True,
    lambda user, payload: _tender_action(refresh_document_analysis, user, payload, "tender_id"),
    source="tender_selection.services.refresh_document_analysis",
))

register_tool(AssistantTool(
    "tender.document_proposals_all", "Принять или отклонить все правки товаров",
    "Одной транзакцией принимает или отклоняет весь показанный список: уточнение товара с комплектацией либо состав набора, включая пересмотр прежней разбивки. Исходные данные и общая цена сохраняются; изменившийся состав требует нового просмотра. Подтверждения без изменений не требуют согласования; список содержит только реальные правки, включая подтверждённые требования 1875 с источниками.",
    {"tender_id": {"type": "integer", "required": True, "reference": {"model": "tender_selection.Tender", "label": ["purchase_number", "title"]}}, "decision": {"type": "accept|reject", "required": True, "label": "Решение", "choices": (("accept", "Принять все"), ("reject", "Отклонить все"))}, "proposal_ids": {"type": "integer[]", "required": True, "label": "Проверенные предложения", "reference": {"model": "tenders.TenderSourceItem", "label": ["original_text"]}}},
    False, True, True,
    lambda user, payload: _tender_action(decide_all_document_proposals, user, payload, "tender_id", "decision", "proposal_ids"),
    source="tender_selection.services.decide_all_document_proposals",
))


register_tool(AssistantTool(
    "tender.import_by_number", "Добавить тендер по номеру",
    "Находит закупку по 44-ФЗ в ГосПлане и добавляет её без дубля. Существующий тендер не меняет; отсутствие данных не создаёт пустую карточку. Фильтры входящих сохраняются.",
    {"purchase_number": {"type": "string", "required": True, "label": "Номер закупки по 44-ФЗ"}},
    False, True, True,
    lambda user, payload: _tender_action(import_tender_by_number, user, payload, "purchase_number"),
    source="tender_selection.services.import_tender_by_number",
))

register_tool(AssistantTool(
    "tender.refresh_status", "Проверить обновление тендера",
    "Читает текущий этап, время и результат обновления без запуска ИИ. Администратору также показывает оценку стоимости полученных ответов этого обновления, включая неудачные; это не точный счёт провайдера. Служебный текст оборванного ответа не выдаётся.",
    {"tender_id": {"type": "integer", "required": True, "reference": {"model": "tender_selection.Tender", "label": ["purchase_number", "title"]}}},
    True, False, False,
    lambda user, payload: _tender_action(tender_refresh_status_for, user, payload, "tender_id"),
    source="tender_selection.services.tender_refresh_status_for",
))

register_tool(AssistantTool(
    "tender.item_route", "Показать сохранённый маршрут товара",
    "Читает тот же маршрут, что выдвижное окно позиции: этапы, актуальность и требующие уточнения сведения. Не запускает ИИ и не создаёт расчёт.",
    {"tender_id": {"type": "integer", "required": True, "reference": {"model": "tender_selection.Tender", "label": ["purchase_number", "title"]}}, "source_item_id": {"type": "integer", "required": True, "label": "Товарная позиция", "reference": {"model": "tenders.TenderSourceItem", "label": ["original_text"]}}},
    True, False, True, lambda user, payload: _tender_action(source_route_for, user, payload, "tender_id", "source_item_id"),
    source="tender_selection.services.source_route_for",
))
