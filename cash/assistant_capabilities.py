"""Published descriptions of existing cash actions."""
from tenders.assistant_tools import AssistantTool, AssistantToolError, register_tool
from . import actions


def _call(method, *keys):
    def execute(user, payload):
        try:
            values = [payload.get(key) for key in keys]
            return method(user, *values) if keys else method(user, payload)
        except actions.CashActionError as exc:
            raise AssistantToolError(str(exc)) from exc
    return execute


register_tool(AssistantTool("cash.transaction_create", "Создать операцию кассы", "Действие формы кассы с аудитом.", {"operation_date": "YYYY-MM-DD", "account": "cash|card", "direction": "income|expense", "amount": "number", "reason": "string"}, False, True, True, _call(actions.create_transaction), source="cash.actions.create_transaction"))
def update_transaction(user, payload):
    try:
        return actions.update_transaction(user, payload.get("transaction_id"), payload)
    except actions.CashActionError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool("cash.transaction_update", "Изменить операцию кассы", "Действие формы кассы с аудитом.", {"transaction_id": "integer", "operation_date": "YYYY-MM-DD", "account": "cash|card", "direction": "income|expense", "amount": "number", "reason": "string"}, False, True, True, update_transaction, source="cash.actions.update_transaction"))
register_tool(AssistantTool("cash.transaction_delete", "Удалить операцию кассы", "Действие удаления операции кассы с аудитом.", {"transaction_id": "integer"}, False, True, True, _call(actions.delete_transaction, "transaction_id"), source="cash.actions.delete_transaction"))
register_tool(AssistantTool("cash.reconciliation_save", "Сохранить сверку кассы", "Действие формы сверки кассы.", {"effective_date": "YYYY-MM-DD", "cash_balance": "number", "card_balance": "number", "note": "string?"}, False, True, True, _call(actions.save_reconciliation), source="cash.actions.save_reconciliation"))
register_tool(AssistantTool("cash.bank_payment_visibility_toggle", "Изменить видимость банковского платежа", "Действие страницы банковских платежей.", {"bank_payment_id": "integer"}, False, True, True, _call(actions.toggle_bank_payment_visibility, "bank_payment_id"), source="cash.actions.toggle_bank_payment_visibility"))
register_tool(AssistantTool("cash.bank_sync", "Синхронизировать банковские платежи", "Действие штатной синхронизации банка.", {}, False, True, True, lambda u, p: _call(actions.sync_bank)(u, p), source="cash.actions.sync_bank"))
