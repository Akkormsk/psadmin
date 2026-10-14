"""Published references to tender-selection executors shared with the site."""
from tenders.assistant_tools import AssistantTool, AssistantToolError, register_tool

from .services import answer_tender_owner_question, save_selection_settings


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


register_tool(AssistantTool(
    "tender.discovery_settings", "Настроить поиск и отбор закупок",
    "Изменяет действующие правила поиска тем же общим исполнителем, что и настройки сайта.",
    {"include_words": "string?", "exclude_words": "string?", "profile_triage_enabled": "boolean?",
     "min_price": "number?", "window_days": "integer?", "okpd2_codes": "string[]?",
     "regions": "string[]?", "laws": "string[]?"},
    False, True, True, _save_discovery_settings,
    source="tender_selection.services.save_selection_settings",
))

register_tool(AssistantTool(
    "tender.owner_question_answer", "Ответить на запрос уточнения по тендеру",
    "Сохраняет ответ и закрывает вопрос тем же общим исполнителем, что карточка тендера.",
    {"tender_id": "integer", "interaction_id": "integer", "answer": "string"},
    False, True, True, _answer_owner_question,
    source="tender_selection.services.answer_tender_owner_question",
))
