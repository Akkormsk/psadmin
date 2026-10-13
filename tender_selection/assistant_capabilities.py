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
for identifier, name, schema, handler, source in _ACTIONS:
    register_tool(AssistantTool(identifier, name, name, schema, False, True, True, _call(handler), source=source))

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
