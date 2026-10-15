"""Published references to tender-selection executors shared with the site."""
from tenders.assistant_tools import AssistantTool, AssistantToolError, register_tool

from .services import (
    answer_tender_owner_question, change_forecast_inclusion, create_tender_estimate, hide_tender,
    apply_filter_word_audit, decide_document_proposals, pull_tenders_now, run_filter_word_audit, run_profile_triage,
    record_tender_outcome, save_evaluation_settings, save_selection_settings, save_tender_bid,
)


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


def _save_bid(user, payload):
    try:
        return save_tender_bid(user, payload["estimate_id"], bid_number=payload.get("bid_number", ""), bid_price=payload.get("bid_price"))
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


def _no_argument_action(call, user, payload):
    try:
        return call(user)
    except ValueError as exc:
        raise AssistantToolError(str(exc)) from exc


def _apply_word_audit(user, payload):
    try:
        return apply_filter_word_audit(user, payload["changes"])
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
    {"include_words": "string?", "exclude_words": "string?", "profile_triage_enabled": "boolean?",
     "min_price": "number?", "window_days": "integer?", "okpd2_codes": "string[]?",
     "regions": "string[]?", "laws": "string[]?"},
    False, True, True, _save_discovery_settings,
    source="tender_selection.services.save_selection_settings",
))
register_tool(AssistantTool(
    "tender.outcome_record", "Получить или сохранить результат торгов", "Проверяет протокол/контракт либо сохраняет явно выбранный итог торгов тем же общим исполнителем, что карточка тендера.",
    {"estimate_id": "integer", "status": "won|lost|not_participated?", "actual_reduction_percent": "number?"},
    False, True, True, _record_outcome, source="tender_selection.services.record_tender_outcome",
))
register_tool(AssistantTool(
    "tender.word_audit_run", "Запустить аудит плюс- и минус-слов", "Запускает ту же проверку правил отбора, что кнопка страницы аудита.",
    {}, False, True, True, lambda user, payload: _no_argument_action(run_filter_word_audit, user, payload),
    source="tender_selection.services.run_filter_word_audit",
))
register_tool(AssistantTool(
    "tender.word_audit_apply", "Применить выбранные предложения аудита слов", "Применяет только явно выбранные изменения фильтра через тот же исполнитель, что страница аудита.",
    {"changes": "object"}, False, True, True, _apply_word_audit,
    source="tender_selection.services.apply_filter_word_audit",
))
register_tool(AssistantTool(
    "tender.profile_triage_run", "Запустить проверку входящих тендеров", "Запускает штатную фоновую проверку до 100 доступных входящих тендеров.",
    {}, False, True, True, lambda user, payload: _no_argument_action(run_profile_triage, user, payload),
    source="tender_selection.services.run_profile_triage",
))
register_tool(AssistantTool(
    "tender.pull", "Получить новые тендеры", "Запускает ту же ограниченную выгрузку из ГосПлан, что кнопка входящих тендеров.",
    {}, False, True, True, lambda user, payload: _no_argument_action(pull_tenders_now, user, payload),
    source="tender_selection.services.pull_tenders_now",
))

register_tool(AssistantTool(
    "tender.hide", "Скрыть неподходящий тендер", "Архивирует тендер тем же исполнителем, что кнопка интерфейса, и сохраняет причину для входящего тендера.",
    {"tender_id": "integer", "reason": "string?"}, False, True, True, _hide_tender,
    source="tender_selection.services.hide_tender",
))
register_tool(AssistantTool(
    "tender.forecast_toggle", "Включить или исключить тендер из прогноза", "Переключает существующую запись статистики тем же правилом, что архив тендеров.",
    {"tender_id": "integer"}, False, True, True,
    lambda user, payload: _tender_action(change_forecast_inclusion, user, payload, "tender_id"),
    source="tender_selection.services.change_forecast_inclusion",
))
register_tool(AssistantTool(
    "tender.send_to_calculation", "Передать тендер в расчёт", "Создаёт или возвращает существующий расчёт тендера через штатный сервис воронки.",
    {"tender_id": "integer"}, False, True, True,
    lambda user, payload: _tender_action(create_tender_estimate, user, payload, "tender_id"),
    source="tender_selection.services.create_tender_estimate",
))
register_tool(AssistantTool(
    "tender.bid_save", "Сохранить нашу ставку по тендеру", "Сохраняет номер и сумму нашей заявки тем же сервисом, по которому система сопоставляет протокол торгов.",
    {"estimate_id": "integer", "bid_number": "string?", "bid_price": "number?"}, False, True, True, _save_bid,
    source="tender_selection.services.save_tender_bid",
))

register_tool(AssistantTool(
    "tender.evaluation_settings", "Сохранить правила оценки тендеров",
    "Сохраняет пороги риска, ROI, НДС и прогноз тем же валидатором, что страница настроек оценки.",
    {"risk_warning_days": "integer", "risk_critical_days": "integer", "roi_good_percent": "number",
     "roi_thin_percent": "number", "vat_rate": "number", "default_reduction_percent": "number",
     "stats_target_count": "integer", "stats_min_samples": "integer", "reduction_hint_min": "integer",
     "reduction_hint_max": "integer", "incoming_ttl_days": "integer", "level_samples_required": "low|medium|high",
     "level_samples_impossible": "low|medium|high", "level_national_confirmation": "low|medium|high",
     "level_national_blocked": "low|medium|high", "level_delivery_requests": "low|medium|high",
     "level_delivery_open_ended": "low|medium|high"},
    False, True, True, _save_evaluation_settings,
    source="tender_selection.services.save_evaluation_settings",
))

register_tool(AssistantTool(
    "tender.owner_question_answer", "Ответить на запрос уточнения по тендеру",
    "Сохраняет ответ и закрывает вопрос тем же общим исполнителем, что карточка тендера.",
    {"tender_id": "integer", "interaction_id": "integer", "answer": "string"},
    False, True, True, _answer_owner_question,
    source="tender_selection.services.answer_tender_owner_question",
))
register_tool(AssistantTool(
    "tender.document_proposals", "Принять или отклонить состав из документации",
    "Применяет к позиции тендера найденный в документации состав либо отклоняет его тем же исполнителем, что карточка тендера.",
    {"tender_id": "integer", "parent_id": "integer", "decision": "accept|reject"},
    False, True, True,
    lambda user, payload: _tender_action(
        decide_document_proposals, user, payload, "tender_id", "parent_id", "decision",
    ),
    source="tender_selection.services.decide_document_proposals",
))
