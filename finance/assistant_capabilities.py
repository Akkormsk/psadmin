"""Published descriptions of finance actions shared with the UI."""
from tenders.assistant_tools import AssistantTool, AssistantToolError, register_tool
from . import actions


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


_FIELDS = {"record_type": "order|design", "order_number": "string", "gross_profit": "number", "accounting_period": "YYYY-MM", "manager": "user id for owner"}
register_tool(AssistantTool("finance.order_record_create", "Создать закрытый заказ", "Действие существующей финансовой формы.", _FIELDS, False, True, True, create, source="finance.actions.create_order_record"))
register_tool(AssistantTool("finance.order_record_update", "Изменить закрытый заказ", "Действие существующей финансовой формы.", {"order_record_id": "integer", **_FIELDS}, False, True, True, update, source="finance.actions.update_order_record"))
register_tool(AssistantTool("finance.order_record_delete", "Удалить закрытый заказ", "Действие существующей финансовой формы.", {"order_record_id": "integer"}, False, True, True, delete, source="finance.actions.delete_order_record"))
