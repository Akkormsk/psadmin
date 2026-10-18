"""Published descriptions of finance actions shared with the UI."""
from tenders.assistant_tools import AssistantResource, AssistantTool, AssistantToolError, register_resource, register_tool
from . import actions


register_resource(AssistantResource(
    "finance.periods", "Финансовые периоды",
    "Учётные периоды и их состояние: открыты они или закрыты.",
    "finance.FinancialPeriod", ("finance",),
))
register_resource(AssistantResource(
    "finance.operating_expenses", "Постоянные расходы",
    "Регулярные расходы, учитываемые в финансовых расчётах.",
    "finance.OperatingExpense", ("finance",),
))
register_resource(AssistantResource(
    "finance.payroll_lines", "Начисления зарплаты",
    "Строки начислений сотрудникам по финансовым периодам.",
    "finance.PayrollLine", ("finance", "payroll"),
))


def create(user, payload):
    try:
        return actions.create_order_record(user, payload)
    except actions.FinanceActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def update(user, payload):
    try:
        return actions.update_order_record(user, payload.get("order_record_id"), payload)
    except actions.FinanceActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def delete(user, payload):
    try:
        return actions.delete_order_record(user, payload.get("order_record_id"))
    except actions.FinanceActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def save_expenses(user, payload):
    try:
        return actions.save_operating_expenses(user, payload)
    except actions.FinanceActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def delete_expense(user, payload):
    try:
        return actions.delete_operating_expense(user, payload.get("expense_id"))
    except actions.FinanceActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def set_period_state(user, payload):
    if not isinstance(payload.get("closed"), bool):
        raise AssistantToolError("Для закрытия периода нужно явно указать true или false")
    try:
        return actions.set_financial_period_state(
            user, payload["period"], closed=payload["closed"],
        )
    except actions.FinanceActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def save_managers(user, payload):
    try:
        return actions.save_manager_settings(user, payload)
    except actions.FinanceActionError as exc:
        raise AssistantToolError(str(exc)) from exc


def save_payroll(user, payload):
    try:
        return actions.save_payroll_lines(user, payload["period"], payload["updates"])
    except actions.FinanceActionError as exc:
        raise AssistantToolError(str(exc)) from exc


_FIELDS = {"record_type": "order|design", "order_number": "string", "gross_profit": "number", "accounting_period": "YYYY-MM", "manager": "user id for owner"}
register_tool(AssistantTool("finance.order_record_create", "Создать закрытый заказ", "Действие существующей финансовой формы.", _FIELDS, False, True, True, create, source="finance.actions.create_order_record", areas=("finance", "payroll")))
register_tool(AssistantTool("finance.order_record_update", "Изменить закрытый заказ", "Действие существующей финансовой формы.", {"order_record_id": "integer", **_FIELDS}, False, True, True, update, source="finance.actions.update_order_record", areas=("finance", "payroll")))
register_tool(AssistantTool("finance.order_record_delete", "Удалить закрытый заказ", "Действие существующей финансовой формы.", {"order_record_id": "integer"}, False, True, True, delete, source="finance.actions.delete_order_record", areas=("finance", "payroll")))
register_tool(AssistantTool("finance.operating_expenses_save", "Сохранить постоянные расходы", "Добавляет или изменяет постоянные расходы тем же исполнителем, что и страница финансов.", {"updates": "array?", "new": "object?"}, False, True, True, save_expenses, source="finance.actions.save_operating_expenses"))
register_tool(AssistantTool("finance.operating_expense_delete", "Удалить постоянный расход", "Удаляет постоянный расход тем же исполнителем, что и страница финансов.", {"expense_id": "integer"}, False, True, True, delete_expense, source="finance.actions.delete_operating_expense"))
register_tool(AssistantTool("finance.manager_settings_save", "Сохранить настройки менеджеров и KPI", "Сохраняет ставки, порядок и KPI тем же исполнителем, что и страница финансов.", {"employee_order": "integer[]?", "managers": "array"}, False, True, True, save_managers, source="finance.actions.save_manager_settings"))

register_tool(AssistantTool("finance.period_lock", "Закрыть или открыть финансовый период",
    "Закрывает проверенный расчётный период либо вновь разрешает его редактирование. Существующая финансовая страница использует тот же исполнитель.",
    {"period": "YYYY-MM", "closed": "boolean"}, False, True, True,
    set_period_state, source="finance.actions.set_financial_period_state"))
register_tool(AssistantTool("finance.payroll_save", "Сохранить данные зарплаты за период",
    "Сохраняет доступные пользователю смены и начисления, затем пересчитывает тот же финансовый период, что страница зарплаты.",
    {"period": "YYYY-MM", "updates": "array"}, False, True, False,
    save_payroll, source="finance.actions.save_payroll_lines", areas=("finance", "payroll")))
