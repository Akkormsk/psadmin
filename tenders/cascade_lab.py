"""Исполнитель фиксированного графа Cascade для отладки и повторных прогонов."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from decimal import Decimal

from .cascade import Cascade, Criterion
from .gateway_budget import preflight, spend_rub
from .models import CatalogProduct


STEP_DEFINITIONS = [
    {"step": 1, "method": "step_1_parse_tz", "title": "Разбор ТЗ", "input": "JSON-ТЗ", "output": "Критерии ТЗ"},
    {"step": 2, "method": "step_2_search_plan", "title": "Чистка названия", "input": "Исходное название", "output": "Чистое название и поисковые фразы"},
    {"step": 3, "method": "step_3_search_by_name", "title": "Поиск по каталогам", "input": "Поисковые фразы", "output": "Пул товаров"},
    {"step": 4, "method": "step_4_name_filter", "title": "Отсев названий", "input": "Пул товаров", "output": "Подходящие типы товаров"},
    {"step": 5, "method": "step_5_hard_gates_and_collapse", "title": "Фильтры и варианты", "input": "Товары каталога", "output": "Карточки групп"},
    {"step": 6, "method": "step_6_agent_matrix", "title": "Матрица ТЗ", "input": "Карточки и критерии", "output": "Матрица да / нет / НЗ"},
    {"step": 7, "method": "step_7_collapse_and_sort", "title": "Сортировка", "input": "Проверенные карточки", "output": "Ранжированный список"},
    {"step": 8, "method": "step_8_price_and_top", "title": "Цена и топ-10", "input": "Ранжированный список", "output": "Итоговая выдача"},
]


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_value(value):
    if isinstance(value, Criterion):
        value = asdict(value)
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


def _encode_output(value, *, include_matrix=True):
    rows = list(value) if not isinstance(value, list) and hasattr(value, "__iter__") else value
    if isinstance(rows, list) and rows and isinstance(rows[0], CatalogProduct):
        return _json_value({
            "kind": "catalog_products",
            "ids": [row.pk for row in rows],
            "preview": [
                {"pk": row.pk, "id": row.external_id, "name": row.full_name or row.name,
                 "supplier": row.supplier.code, "price": row.effective_price, "stock": row.total_stock}
                for row in rows[:50]
            ],
            "count": len(rows),
        })
    if isinstance(rows, list):
        if not include_matrix:
            return [
                _json_value({key: cell for key, cell in item.items() if key != "matrix"})
                if isinstance(item, dict) else _json_value(item)
                for item in rows
            ]
        return [_json_value(item) for item in rows]
    return _json_value(rows)


def _decode_output(value):
    if isinstance(value, dict) and value.get("kind") == "catalog_products":
        products = CatalogProduct.objects.select_related("supplier").in_bulk(value.get("ids", []))
        ordered = [products[pk] for pk in value.get("ids", []) if pk in products]
        family_keys = {product.family_key for product in ordered if product.family_key}
        variants_by_family = {}
        if family_keys:
            variants = CatalogProduct.objects.select_related("supplier").filter(
                is_active=True, family_key__in=family_keys,
            )
            for variant in variants:
                variants_by_family.setdefault((variant.supplier_id, variant.family_key), []).append(variant)
        for product in ordered:
            product._variant_products = variants_by_family.get(
                (product.supplier_id, product.family_key), [product],
            )
        return ordered
    return value


def _cascade_state(cascade):
    return _json_value({
        "item": cascade.item, "queries": cascade.queries, "tz": [asdict(c) for c in cascade.tz],
        "feedback_instructions": cascade.feedback_instructions,
        "feedback_instructions_result": getattr(cascade, "feedback_instructions_result", []),
        "usage": cascade.usage, "usage_by_model": cascade.usage_by_model,
        "sources": cascade.sources, "diagnostics": cascade.diagnostics,
        "ranking": cascade.ranking, "oasis_mirror": cascade._oasis_mirror,
        "tz_hash": cascade._tz_hash, "error": cascade.error,
    })


def _restore_state(cascade, state):
    cascade.item = state.get("item", "")
    cascade.queries = list(state.get("queries", []))
    cascade.tz = []
    for raw in state.get("tz", []):
        data = dict(raw)
        for key in ("num_min", "num_max"):
            data[key] = Decimal(str(data[key])) if data.get(key) not in (None, "") else None
        cascade.tz.append(Criterion(**data))
    cascade.feedback_instructions = list(state.get("feedback_instructions", []))
    cascade.feedback_instructions_result = list(state.get("feedback_instructions_result", []))
    cascade.usage = dict(state.get("usage", {"prompt_tokens": 0, "completion_tokens": 0}))
    cascade.usage_by_model = dict(state.get("usage_by_model", {}))
    cascade.sources = dict(state.get("sources", cascade.sources))
    cascade.diagnostics = dict(state.get("diagnostics", {"verdict_cache_hits": 0}))
    cascade.ranking = dict(state.get("ranking", {}))
    cascade._oasis_mirror = bool(state.get("oasis_mirror"))
    cascade._tz_hash = state.get("tz_hash", "")
    cascade.error = state.get("error", "")


def _usage_delta(before, after):
    result = {}
    for model in set(before) | set(after):
        old, new = before.get(model, {}), after.get(model, {})
        result[model] = {
            "prompt_tokens": max(0, (new.get("prompt_tokens", 0) or 0) - (old.get("prompt_tokens", 0) or 0)),
            "completion_tokens": max(0, (new.get("completion_tokens", 0) or 0) - (old.get("completion_tokens", 0) or 0)),
        }
    return result


def _cost(usage_by_model):
    values = [spend_rub(usage, model) for model, usage in usage_by_model.items()]
    known = [value for value in values if value is not None]
    return round(sum(known), 4) if known else 0


def evaluate_expectations(cards, expectations, *, total_seconds, total_cost_rub):
    cards = cards if isinstance(cards, list) else []
    ids = [str(card.get("id")) for card in cards if isinstance(card, dict)]
    checks = []
    for expected in expectations.get("must_include", []):
        checks.append({"label": f"В выдаче есть {expected}", "passed": str(expected) in ids})
    for expected in expectations.get("must_exclude", []):
        checks.append({"label": f"В выдаче нет {expected}", "passed": str(expected) not in ids})
    for card_id, bounds in expectations.get("ranks", {}).items():
        rank = ids.index(str(card_id)) + 1 if str(card_id) in ids else None
        passed = rank is not None and int(bounds.get("min", 1)) <= rank <= int(bounds.get("max", len(ids) or 1))
        checks.append({"label": f"Место {card_id}: {bounds.get('min', 1)}–{bounds.get('max', len(ids))}", "passed": passed, "actual": rank})
    by_id = {str(card.get("id")): card for card in cards if isinstance(card, dict)}
    for card_id, fields in expectations.get("matrix", {}).items():
        card = by_id.get(str(card_id))
        for field, expected in fields.items():
            actual = card.get(field) if card else None
            checks.append({"label": f"{card_id}.{field} = {expected}", "passed": actual == expected, "actual": actual})
    if expectations.get("max_seconds") is not None:
        checks.append({"label": f"Не дольше {expectations['max_seconds']} с", "passed": total_seconds <= float(expectations["max_seconds"]), "actual": total_seconds})
    if expectations.get("max_cost_rub") is not None:
        checks.append({"label": f"Не дороже {expectations['max_cost_rub']} ₽", "passed": total_cost_rub <= float(expectations["max_cost_rub"]), "actual": total_cost_rub})
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def _lessons_provider(item, labels):
    from .services import _retrieve_lessons
    return _retrieve_lessons("catalog", item, labels)


def _skip_labels():
    from .services import _requirement_skip_labels
    return {row["label_normalized"] for row in _requirement_skip_labels()}


def execute_cascade_steps(*, line, settings, from_step=1, stop_after=8, snapshots=None,
                          cascade_state=None, expectations=None, prior_total_seconds=None,
                          prior_total_cost_rub=None):
    """Выполняет выбранный диапазон без сохранения прогонов в базе."""
    preflight()
    from_step = max(1, min(8, int(from_step)))
    stop_after = max(from_step, min(8, int(stop_after)))
    snapshots = [item for item in (snapshots or []) if int(item.get("step", 0)) < from_step]
    state = snapshots[-1].get("state", {}) if snapshots else (cascade_state or {})
    cascade = Cascade(
        line, lessons_provider=_lessons_provider, skip_labels=_skip_labels(),
        top=max(1, min(50, int(settings.get("top", 10) or 10))),
        step_settings=settings.get("steps", {}),
        max_cost_rub=float(settings.get("max_cost_rub", 0) or 0),
    )
    _restore_state(cascade, state)
    previous = _decode_output(snapshots[-1]["output"]) if snapshots else None
    snapshot_seconds = sum(float(item.get("metrics", {}).get("seconds", 0) or 0) for item in snapshots)
    snapshot_cost = sum(float(item.get("metrics", {}).get("cost_rub", 0) or 0) for item in snapshots)
    total_seconds = snapshot_seconds if prior_total_seconds is None else max(0, float(prior_total_seconds))
    total_cost = round(snapshot_cost if prior_total_cost_rub is None else max(0, float(prior_total_cost_rub)), 4)
    time_limit = float(settings.get("max_seconds", 0) or 0)
    cost_limit = float(settings.get("max_cost_rub", 0) or 0)
    pause_reason = ""
    if time_limit:
        cascade.deadline = time.perf_counter() + max(0, time_limit - total_seconds)

    for definition in STEP_DEFINITIONS[from_step - 1:stop_after]:
        if cost_limit and total_cost >= cost_limit:
            pause_reason = f"Достигнут лимит {cost_limit:g} ₽"
            break
        if time_limit and total_seconds >= time_limit:
            pause_reason = f"Достигнут лимит {time_limit:g} секунд"
            break
        step = definition["step"]
        input_data = line.get("requirements", {}) if step == 1 else {"name": line.get("name", "")} if step == 2 else _encode_output(previous)
        before_usage = _json_value(cascade.usage_by_model)
        before_error = cascade.error
        started = time.perf_counter()
        skipped = False
        custom_cards = settings.get("custom_cards")
        if custom_cards and step in {3, 4}:
            output, skipped = {"custom_cards_bypass": True, "count": len(custom_cards)}, True
        elif custom_cards and step == 5:
            output = _json_value(custom_cards)
        elif step in {1, 2}:
            output = getattr(cascade, definition["method"])()
        else:
            output = getattr(cascade, definition["method"])(_decode_output(previous))
        seconds = time.perf_counter() - started
        encoded = _encode_output(output, include_matrix=step != 7)
        usage = _usage_delta(before_usage, cascade.usage_by_model)
        cost = _cost(usage)
        total_seconds += seconds
        total_cost = round(total_cost + cost, 4)
        step_error = cascade.error if cascade.error and cascade.error != before_error else ""
        snapshot = {
            "step": step, "method": definition["method"], "title": definition["title"],
            "status": "skipped" if skipped else "fallback" if step_error else "completed",
            "error": step_error, "input": input_data, "output": encoded,
            "state": _cascade_state(cascade),
            "metrics": {"seconds": round(seconds, 4), "cost_rub": cost, "usage_by_model": usage,
                        "input_count": _count_payload(input_data), "output_count": _count_payload(encoded)},
        }
        snapshots.append(snapshot)
        previous = encoded
        if step_error and "лимит" in step_error.lower() and step < stop_after:
            pause_reason = step_error
            break
        if cost_limit and total_cost >= cost_limit and step < stop_after:
            pause_reason = f"Достигнут лимит {cost_limit:g} ₽"
            break
        if time_limit and total_seconds >= time_limit and step < stop_after:
            pause_reason = f"Достигнут лимит {time_limit:g} секунд"
            break

    current_step = int(snapshots[-1]["step"]) if snapshots else from_step - 1
    result = {"pause_reason": pause_reason} if pause_reason else {}
    if current_step == 8:
        result = evaluate_expectations(
            _decode_output(previous), expectations or {},
            total_seconds=total_seconds, total_cost_rub=total_cost,
        )
    return {
        "status": "completed" if current_step == 8 else "paused",
        "current_step": current_step, "stop_after": stop_after,
        "snapshots": snapshots, "cascade_state": _cascade_state(cascade),
        "input_payload": line, "settings": settings, "result": result,
        "total_seconds": round(total_seconds, 4), "total_cost_rub": total_cost,
        "error": "",
    }


def _count_payload(value):
    if isinstance(value, dict) and "count" in value:
        return value["count"]
    if isinstance(value, (list, dict)):
        return len(value)
    return 1 if value is not None else 0
