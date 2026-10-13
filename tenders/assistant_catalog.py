"""Runtime encyclopedia: shared backend capabilities grouped by user goals.

The grouping contains no business execution logic. Concrete actions remain
registered by their owning apps; the registry is the sole executable source.
"""
from __future__ import annotations

from collections import defaultdict


# User-facing descriptions of multi-step goals; each references existing tools.
# New registrations are included automatically under their application domain.
_GOALS = {
    "calculator": "Управлять расчётами: рассчитать, создать, изменить, копировать или удалить. Сохранение использует штатную логику калькулятора.",
    "cash": "Вести кассу и банк: записывать и исправлять операции, сверять остатки, синхронизировать платежи. Штатный аудит выполняется автоматически.",
    "finance": "Управлять финансовым учётом: закрытые заказы, расходы, ставки и настройки менеджеров.",
    "estimate": "Управлять сохранёнными тендерными и заказными расчётами: копировать, удалять и менять стадии через общий бэкенд.",
    "production": "Подтверждать выбранные исправления обучения и отключать выученные производственные правила с сохранением истории.",
    "tender": "Вести тендеры: обновлять входящие, оценивать, классифицировать, передавать в расчёт, учитывать ставки и результаты.",
    "provider": "Работать с контрагентами, их прайсами и подтверждёнными калькуляторами. Не активировать неподтверждённые знания.",
    "process": "Просматривать доступные производственные операции и технологические этапы.",
    "read": "Получать данные и рассчитывать статистику по разрешённым полям без изменения записей.",
    "assistant": "Просматривать возможности ассистента и доступные бизнес-данные.",
}


def model_catalog():
    """The exact read schema used by the validated ORM executor."""
    from .assistant_read import catalogue

    return catalogue()


def action_catalog(user):
    from .assistant_tools import available_tools

    return available_tools(user)


def workflow_catalog(user, actions=None):
    """Group existing declarations without introducing duplicate executors."""
    actions = action_catalog(user) if actions is None else actions
    grouped = defaultdict(list)
    for action in actions:
        namespace = action["id"].split(".", 1)[0]
        grouped[namespace].append(action)
    result = []
    for namespace, members in sorted(grouped.items()):
        result.append({
            "id": namespace,
            "description": _GOALS.get(namespace, f"Операции раздела {namespace}."),
            "actions": [entry["id"] for entry in members],
            "available": any(entry["available"] for entry in members),
            "requires_confirmation": any(entry["requires_confirmation"] for entry in members),
        })
    return result


def site_route_inventory():
    """Live URL inventory for coverage audits, not a list of executable AI tools."""
    from django.urls import URLPattern, URLResolver, get_resolver

    routes = []

    def walk(patterns, prefix=""):
        for entry in patterns:
            route = prefix + str(entry.pattern)
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, route)
                continue
            if not isinstance(entry, URLPattern):
                continue
            callback = entry.callback
            module = getattr(callback, "__module__", "")
            if not module.startswith(("tenders.", "tender_selection.", "calculator.", "cash.", "finance.", "core.", "payroll.")):
                continue
            routes.append({
                "route": route,
                "name": entry.name or "",
                "view": f"{module}.{getattr(callback, '__name__', '')}",
            })

    walk(get_resolver().url_patterns)
    return sorted(routes, key=lambda item: (item["route"], item["name"]))


def catalog(user):
    actions = action_catalog(user)
    output = {"models": model_catalog(), "actions": actions, "workflows": workflow_catalog(user, actions)}
    if user.is_superuser:
        output["site_route_inventory"] = site_route_inventory()
    return output
