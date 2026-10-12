"""Published calculator capabilities backed by existing calculator services."""
from tenders.assistant_tools import AssistantTool, AssistantToolError, register_tool
from .assistant_actions import CalculatorActionError as CalculationInputError, calculate_sheet
from .actions import CalculatorActionError as SavedEstimateError, delete_estimate, duplicate_estimate


def calculate(user, payload):
    try:
        return calculate_sheet(user, payload)
    except CalculationInputError as exc:
        raise AssistantToolError(str(exc)) from exc


def duplicate(user, payload):
    try:
        return duplicate_estimate(user, payload.get("estimate_id"))
    except SavedEstimateError as exc:
        raise AssistantToolError(str(exc)) from exc


def delete(user, payload):
    try:
        return delete_estimate(user, payload.get("estimate_id"))
    except SavedEstimateError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool("calculator.sheet_calculate", "Рассчитать общий калькулятор", "Существующая детерминированная логика калькулятора без сохранения.", {"calculator_type": "sheet|wide?", "product_quantity": "integer", "work_hours": "number?", "lines": "array"}, True, False, True, calculate, source="calculator.services.calculate_sheet_estimate"))
register_tool(AssistantTool("calculator.estimate_duplicate", "Копировать сохранённый расчёт", "Существующее действие интерфейса калькулятора.", {"estimate_id": "integer"}, False, True, True, duplicate, source="calculator.actions.duplicate_estimate"))
register_tool(AssistantTool("calculator.estimate_delete", "Удалить сохранённый расчёт", "Существующее действие интерфейса калькулятора.", {"estimate_id": "integer"}, False, True, True, delete, source="calculator.actions.delete_estimate"))
