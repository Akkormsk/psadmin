"""Published descriptions of existing cash actions."""
from tenders.assistant_tools import AssistantResource, AssistantTool, AssistantToolError, register_resource, register_tool
from . import actions


def _call(method, *keys):
    def execute(user, payload):
        try:
            values = [payload.get(key) for key in keys]
            return method(user, *values) if keys else method(user, payload)
        except actions.CashActionError as exc:
            raise AssistantToolError(str(exc)) from exc
    return execute


register_resource(AssistantResource(
    "cash.transactions", "Операции по кассе",
    "Поступления, расходы и движения по наличным и карте.",
    "cash.CashTransaction", ("cash",),
))
register_resource(AssistantResource(
    "cash.bank_payments", "Банковские платежи",
    "Платежи, полученные из банка, и их видимость в кассе.",
    "cash.BankPayment", ("cash",),
))
register_resource(AssistantResource(
    "cash.reconciliations", "Сверки остатков",
    "Сохранённые сверки наличных и банковских остатков.",
    "cash.CashReconciliation", ("cash",),
))

_TRANSACTION_FIELDS = {
    "operation_date": {"type": "string", "required": True, "description": "Дата операции в формате ГГГГ-ММ-ДД"},
    "account": {"type": "string", "enum": ["cash", "card"], "required": True, "description": "Счёт: cash — наличные, card — карта"},
    "direction": {"type": "string", "enum": ["income", "expense"], "required": True, "description": "Вид движения: income — поступление, expense — расход"},
    "amount": {"type": "number", "required": True, "description": "Сумма операции"},
    "reason": {"type": "string", "required": True, "source_from_user": True, "description": "Основание операции или номер заказа из сообщения пользователя"},
}

register_tool(AssistantTool("cash.transaction_create", "Создать операцию кассы", "Создаёт поступление или расход по кассе. Перед вызовом собери дату, счёт, вид движения, сумму и основание обычными человеческими вопросами.", _TRANSACTION_FIELDS, False, True, True, _call(actions.create_transaction), ui={"model": "cash.CashTransaction"}, source="cash.actions.create_transaction"))
def update_transaction(user, payload):
    try:
        return actions.update_transaction(user, payload.get("transaction_id"), payload)
    except actions.CashActionError as exc:
        raise AssistantToolError(str(exc)) from exc


register_tool(AssistantTool("cash.transaction_update", "Изменить операцию кассы", "Изменяет существующую операцию кассы с аудитом. Перед вызовом уточни, какую операцию нужно изменить, обычным языком.", {"transaction_id": {"type": "integer", "description": "Идентификатор выбранной операции"}, **_TRANSACTION_FIELDS}, False, True, True, update_transaction, ui={"model": "cash.CashTransaction"}, source="cash.actions.update_transaction"))
register_tool(AssistantTool("cash.transaction_delete", "Удалить операцию кассы", "Действие удаления операции кассы с аудитом.", {"transaction_id": "integer"}, False, True, True, _call(actions.delete_transaction, "transaction_id"), source="cash.actions.delete_transaction"))
register_tool(AssistantTool("cash.reconciliation_save", "Сохранить сверку кассы", "Действие формы сверки кассы.", {"effective_date": "YYYY-MM-DD", "cash_balance": "number", "card_balance": "number", "note": "string?"}, False, True, True, _call(actions.save_reconciliation), ui={"model": "cash.CashReconciliation"}, source="cash.actions.save_reconciliation"))
register_tool(AssistantTool("cash.bank_payment_visibility_toggle", "Изменить видимость банковского платежа", "Действие страницы банковских платежей.", {"bank_payment_id": "integer"}, False, True, True, _call(actions.toggle_bank_payment_visibility, "bank_payment_id"), source="cash.actions.toggle_bank_payment_visibility"))
