"""Published calculator capabilities backed by existing calculator services."""
from tenders.assistant_tools import AssistantTool, AssistantToolError, register_tool
from .actions import CalculatorActionError, bulk_estimates, calculate_estimate, delete_estimate, duplicate_estimate, save_estimate


def calculate(user, payload):
    try:
        calculator_type, product_quantity, _work_hours, _lines, _settings, summary = calculate_estimate(payload)
        return {"calculator_type": calculator_type, "product_quantity": product_quantity, "summary": {key: str(value) for key, value in summary.items()}}
    except CalculatorActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def duplicate(user, payload):
    try:
        return duplicate_estimate(user, payload.get("estimate_id"))
    except CalculatorActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def delete(user, payload):
    try:
        return delete_estimate(user, payload.get("estimate_id"))
    except CalculatorActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def save(user, payload):
    try:
        return save_estimate(user, payload, payload.get("estimate_id"))
    except CalculatorActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def bulk(user, payload):
    try:
        return bulk_estimates(user, payload)
    except CalculatorActionError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool("calculator.sheet_calculate", "Рассчитать общий калькулятор", "Существующая детерминированная логика калькулятора без сохранения.", {"calculator_type": "sheet|wide?", "product_quantity": "integer", "work_hours": "number?", "lines": "array"}, True, False, True, calculate, source="calculator.actions.calculate_estimate"))
register_tool(AssistantTool("calculator.estimate_save", "Сохранить расчёт", "Сохраняет расчёт тем же исполнителем, что и страница калькулятора.", {"estimate_id": "integer?", "name": "string", "comment": "string?", "calculator_type": "sheet|wide?", "product_quantity": "integer", "work_hours": "number?", "lines": "array"}, False, True, True, save, source="calculator.actions.save_estimate"))
register_tool(AssistantTool("calculator.estimate_duplicate", "Копировать сохранённый расчёт", "Существующее действие интерфейса калькулятора.", {"estimate_id": "integer"}, False, True, True, duplicate, source="calculator.actions.duplicate_estimate"))
register_tool(AssistantTool("calculator.estimate_delete", "Удалить сохранённый расчёт", "Существующее действие интерфейса калькулятора.", {"estimate_id": "integer"}, False, True, True, delete, source="calculator.actions.delete_estimate"))
register_tool(AssistantTool("calculator.estimate_bulk", "Сгруппировать, разгруппировать или удалить сохранённые расчёты", "Одно массовое действие страницы расчётов; применяет его только к доступным пользователю расчётам.", {"action": "group|ungroup|delete", "estimate_ids": "integer[]", "name": "string?"}, False, True, True, bulk, source="calculator.actions.bulk_estimates"))
