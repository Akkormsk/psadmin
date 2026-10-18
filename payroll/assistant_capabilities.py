"""Published read resource for the payroll application."""
from tenders.assistant_tools import AssistantResource, register_resource


register_resource(AssistantResource(
    "payroll.order_records", "Заказы и макеты для зарплаты",
    "Закрытые заказы и макеты, учитываемые в прибыли менеджеров.",
    "payroll.OrderRecord", ("payroll",),
))
