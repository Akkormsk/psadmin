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
    ("tender.pull", "Обновить тендеры", {"max_requests": "integer?"}, "pull"),
    ("tender.push_to_calculation", "Передать тендер в расчёт", {"tender_id": "integer"}, "push_to_calculation"),
    ("tender.risk_assessment", "Оценить риски тендера", {"tender_id": "integer"}, "start_risk"),
    ("tender.archive", "Архивировать тендер", {"tender_id": "integer"}, "archive"),
    ("tender.dismiss", "Скрыть тендер", {"tender_id": "integer", "reason": "string?"}, "dismiss"),
    ("tender.restore", "Восстановить тендер", {"tender_id": "integer"}, "restore"),
    ("tender.review_set", "Изменить статус проверки тендера", {"tender_id": "integer", "review": "unreviewed|interesting"}, "set_review"),
    ("tender.bid_save", "Сохранить ставку по тендеру", {"estimate_id": "integer", "bid_number": "string?", "bid_price": "number?"}, "save_bid"),
    ("tender.outcome_set", "Зафиксировать итог тендера", {"estimate_id": "integer", "status": "won|lost|not_participated", "actual_reduction_percent": "number?"}, "set_outcome"),
    ("tender.filter_words_update", "Изменить плюс- и минус-слова", {"include_words": "string", "exclude_words": "string"}, "update_filter_words"),
    ("tender.word_audit_run", "Запустить аудит слов", {}, "word_audit_run"),
    ("tender.word_audit_apply", "Применить результаты аудита слов", {"add_plus": "string[]", "add_minus": "string[]", "remove_plus": "string[]", "remove_minus": "string[]"}, "word_audit_apply"),
    ("tender.forecast_toggle", "Изменить участие результата в прогнозе", {"tender_id": "integer"}, "toggle_forecast"),
)
for identifier, name, schema, handler in _ACTIONS:
    register_tool(AssistantTool(identifier, name, name, schema, False, True, True, _call(handler), source=f"tender_selection.services.{handler}"))
