"""Published metadata for existing tender-selection entry points.

Temporary bridge while the remaining view-only execution paths are moved into
the shared services module.  This module owns publication; the agent core does
not enumerate tender actions.
"""
from tenders.assistant_tools import AssistantTool, AssistantToolError, register_tool
from . import assistant_actions


def _call(name):
    def execute(user, payload):
        try:
            return getattr(assistant_actions, name)(user, payload)
        except ValueError as exc:
            raise AssistantToolError(str(exc)) from exc
    return execute


_ACTIONS = (
    ("tender.pull", "Обновить тендеры", {"max_requests": "integer?"}, "pull", "tender_selection.services.run_pull"),
    ("tender.push_to_calculation", "Передать тендер в расчёт", {"tender_id": "integer"}, "push_to_calculation", "tender_selection.services.push_to_estimate"),
    ("tender.risk_assessment", "Оценить риски тендера", {"tender_id": "integer"}, "start_risk", "tender_selection.services.start_risk_assessment_in_background"),
    ("tender.archive", "Архивировать тендер", {"tender_id": "integer"}, "archive", "tender_selection.services.archive_tender"),
    ("tender.dismiss", "Скрыть тендер", {"tender_id": "integer", "reason": "string?"}, "dismiss", "tender_selection.services.dismiss_tender"),
    ("tender.restore", "Восстановить тендер", {"tender_id": "integer"}, "restore", "tender_selection.services.restore_tender"),
    ("tender.review_set", "Изменить статус проверки тендера", {"tender_id": "integer", "review": "unreviewed|interesting"}, "set_review", "tender_selection.services.set_tender_review"),
    ("tender.bid_save", "Сохранить ставку по тендеру", {"estimate_id": "integer", "bid_number": "string?", "bid_price": "number?"}, "save_bid", "tender_selection.services.set_our_bid"),
    ("tender.outcome_set", "Зафиксировать итог тендера", {"estimate_id": "integer", "status": "won|lost|not_participated", "actual_reduction_percent": "number?"}, "set_outcome", "tender_selection.services.apply_tender_outcome"),
    ("tender.filter_words_update", "Изменить плюс- и минус-слова", {"include_words": "string", "exclude_words": "string"}, "update_filter_words", "tender_selection.services.update_filter_words"),
    ("tender.word_audit_run", "Запустить аудит слов", {}, "word_audit_run", "tender_selection.word_audit.run_audit"),
    ("tender.word_audit_apply", "Применить результаты аудита слов", {"add_plus": "string[]", "add_minus": "string[]", "remove_plus": "string[]", "remove_minus": "string[]"}, "word_audit_apply", "tender_selection.word_audit.apply_words"),
    ("tender.forecast_toggle", "Изменить участие результата в прогнозе", {"tender_id": "integer"}, "toggle_forecast", "tender_selection.services.toggle_forecast_inclusion"),
)
_ACTION_DESCRIPTIONS = {
    "tender.pull": "Получить новые и изменённые закупки из подключённого источника, обновить существующие записи и сохранить результат выгрузки.",
    "tender.push_to_calculation": "Создать расчёт тендера с позициями из извещения и перевести его на этап подготовки цены.",
    "tender.risk_assessment": "Запустить штатную фоновую оценку юридических, коммерческих и сроковых рисков конкретной закупки.",
    "tender.archive": "Архивировать выбранную закупку с сохранением истории и связанных данных.",
    "tender.dismiss": "Скрыть неинтересную закупку, сохранить причину и выполнить штатную очистку связанных предпросмотров.",
    "tender.restore": "Вернуть ранее скрытую закупку в рабочий список без создания копии.",
    "tender.review_set": "Изменить ручную отметку проверки входящей закупки на «интересно» либо «не рассмотрено».",
    "tender.bid_save": "Сохранить номер и цену нашей заявки для конкретного просчёта тендера.",
    "tender.outcome_set": "Ручная отметка результата закупки; не является официальной проверкой протокола ЕИС и должна быть подтверждена владельцем.",
    "tender.filter_words_update": "Обновить включающие и исключающие слова поиска с немедленным использованием штатной фильтрации.",
    "tender.word_audit_run": "Запустить AI-аудит поисковых слов и создать предложения по их изменению; расходует бюджет модели.",
    "tender.word_audit_apply": "Принять выбранные рекомендации аудита: добавить или удалить плюс- и минус-слова.",
    "tender.forecast_toggle": "Включить или исключить зарегистрированный результат закупки из статистики прогноза снижения цены.",
}
for identifier, name, schema, handler, source in _ACTIONS:
    register_tool(AssistantTool(identifier, name, _ACTION_DESCRIPTIONS[identifier], schema, False, True, True, _call(handler), source=source))

def save_discovery_settings(user, payload):
    from .services import save_selection_settings
    try:
        return save_selection_settings(payload)
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool(
    "tender.discovery_settings", "Настроить поиск и отбор закупок",
    "Изменяет фильтры поиска тендеров: ключевые слова, ценовой порог, период, категории ОКПД2, регионы, законы и автоматический отбор. Неуказанные настройки сохраняются без изменений.",
    {"include_words": "string?", "exclude_words": "string?", "profile_triage_enabled": "boolean?",
     "min_price": "number?", "window_days": "integer?", "okpd2_codes": "string[]?",
     "regions": "string[]?", "laws": "string[]?"},
    False, True, True, save_discovery_settings,
    source="tender_selection.services.save_selection_settings",
))

def resolve_owner_question(user, payload):
    from .services import answer_tender_owner_question
    try:
        return answer_tender_owner_question(user, payload["tender_id"],
                                            payload["interaction_id"], payload["answer"])
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool(
    "tender.owner_question_answer", "Ответить на запрос уточнения по тендеру",
    "Сохраняет ответ на открытый вопрос с записью в журнал и закрывает вопрос, как это делает карточка тендера.",
    {"tender_id": "integer", "interaction_id": "integer", "answer": "string"},
    False, True, True, resolve_owner_question,
    source="tender_selection.services.answer_tender_owner_question",
))
