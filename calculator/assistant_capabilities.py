"""Published calculator capabilities backed by existing calculator services."""
from tenders.assistant_tools import AssistantResource, AssistantTool, AssistantToolError, register_resource, register_tool
from .actions import CalculatorActionError, bulk_estimates, calculate_estimate, delete_estimate, duplicate_estimate, save_estimate


register_resource(AssistantResource(
    "calculator.estimates", "Сохранённые расчёты",
    "Расчёты листовой печати и плоттера, сохранённые в калькуляторе.",
    "calculator.Estimate", ("calculator",),
))
register_resource(AssistantResource(
    "calculator.price_items", "Цены и материалы калькулятора",
    "Справочник материалов, работ и цен, используемый калькулятором.",
    "calculator.PriceItem", ("calculator",),
))
register_resource(AssistantResource(
    "calculator.production_rules", "Правила производственного расчёта",
    "Правила расхода материалов и технологических коэффициентов калькулятора.",
    "calculator.ProductionRule", ("calculator",),
))
register_resource(AssistantResource(
    "calculator.settings", "Настройки калькулятора",
    "Ставки, коэффициенты, скидки и НДС листового калькулятора.",
    "calculator.CalculatorSettings", ("calculator",),
))
register_resource(AssistantResource(
    "calculator.estimate_groups", "Группы расчётов",
    "Группы, в которые организованы сохранённые расчёты.",
    "calculator.EstimateGroup", ("calculator",),
))


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
register_tool(AssistantTool("calculator.estimate_save", "Сохранить расчёт", "Сохраняет расчёт тем же исполнителем, что и страница калькулятора.", {'estimate_id': {'type': 'integer', 'required': False, 'reference': {'model': 'calculator.Estimate', 'label': ['name']}}, 'name': 'string', 'comment': 'string?', 'calculator_type': 'sheet|wide?', 'product_quantity': 'integer', 'work_hours': 'number?', 'lines': {'type': 'array', 'required': True, 'label': 'Позиции расчёта'}}, False, True, True, save, ui={'model': 'calculator.Estimate'}, source="calculator.actions.save_estimate"))
register_tool(AssistantTool("calculator.estimate_duplicate", "Копировать сохранённый расчёт", "Существующее действие интерфейса калькулятора.", {'estimate_id': {'type': 'integer', 'required': True, 'reference': {'model': 'calculator.Estimate', 'label': ['name']}}}, False, True, True, duplicate, source="calculator.actions.duplicate_estimate"))
register_tool(AssistantTool("calculator.estimate_delete", "Удалить сохранённый расчёт", "Существующее действие интерфейса калькулятора.", {'estimate_id': {'type': 'integer', 'required': True, 'reference': {'model': 'calculator.Estimate', 'label': ['name']}}}, False, True, True, delete, source="calculator.actions.delete_estimate"))
register_tool(AssistantTool("calculator.estimate_bulk", "Сгруппировать, разгруппировать или удалить сохранённые расчёты", "Одно массовое действие страницы расчётов; применяет его только к доступным пользователю расчётам.", {'action': {'type': 'group|ungroup|delete', 'required': True, 'label': 'Операция с расчётами', 'choices': (('group', 'Сгруппировать'), ('ungroup', 'Разгруппировать'), ('delete', 'Удалить'))}, 'estimate_ids': {'type': 'integer[]', 'required': True, 'reference': {'model': 'calculator.Estimate', 'label': ['name']}}, 'name': {'type': 'string', 'required': False, 'label': 'Название группы'}}, False, True, True, bulk, source="calculator.actions.bulk_estimates"))
