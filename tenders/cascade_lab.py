"""Исполнитель фиксированного графа Cascade для отладки и повторных прогонов."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from decimal import Decimal

from django.db import close_old_connections

from .cascade import Cascade, Criterion
from .gateway_budget import preflight, spend_rub
from .models import CascadeLabRun, CatalogProduct


STEP_DEFINITIONS = [
    {"step": 1, "method": "step_1_parse_tz", "title": "Разбор ТЗ", "input": "Позиция и JSON-ТЗ", "output": "Критерии, название и синонимы"},
    {"step": 2, "method": "step_2_search_plan", "title": "Поисковый план", "input": "Название и синонимы", "output": "Поисковые фразы"},
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


def _encode_output(value):
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
        return [_json_value(item) for item in rows]
    return _json_value(rows)


def _decode_output(value):
    if isinstance(value, dict) and value.get("kind") == "catalog_products":
        products = CatalogProduct.objects.select_related("supplier").in_bulk(value.get("ids", []))
        return [products[pk] for pk in value.get("ids", []) if pk in products]
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


def run_cascade_lab(run_id):
    """Выполнить граф до stop_after, сохраняя снимок после каждого узла."""
    close_old_connections()
    run = CascadeLabRun.objects.get(pk=run_id)
    try:
        preflight()
        run.status, run.error = "running", ""
        run.save(update_fields=["status", "error", "updated_at"])
        cascade = Cascade(
            run.input_payload, lessons_provider=_lessons_provider, skip_labels=_skip_labels(),
            top=max(1, min(50, int(run.settings.get("top", 10) or 10))),
            step_settings=run.settings.get("steps", {}),
        )
        _restore_state(cascade, run.cascade_state)
        previous = _decode_output(run.snapshots[-1]["output"]) if run.snapshots else None
        snapshots = list(run.snapshots)
        pause_reason = ""
        for definition in STEP_DEFINITIONS[run.current_step:run.stop_after]:
            step = definition["step"]
            input_data = run.input_payload if step == 1 else _encode_output(previous)
            before_usage = _json_value(cascade.usage_by_model)
            started = time.perf_counter()
            skipped = False
            custom_cards = run.settings.get("custom_cards")
            if custom_cards and step in {3, 4}:
                output = {"custom_cards_bypass": True, "count": len(custom_cards)}
                skipped = True
            elif custom_cards and step == 5:
                output = _json_value(custom_cards)
            elif step in {1, 2}:
                output = getattr(cascade, definition["method"])()
            else:
                output = getattr(cascade, definition["method"])(_decode_output(previous))
            seconds = time.perf_counter() - started
            encoded = _encode_output(output)
            usage = _usage_delta(before_usage, cascade.usage_by_model)
            cost = _cost(usage)
            snapshot = {
                "step": step, "method": definition["method"], "title": definition["title"],
                "status": "skipped" if skipped else "completed",
                "input": input_data, "output": encoded, "state": _cascade_state(cascade),
                "metrics": {"seconds": round(seconds, 4), "cost_rub": cost, "usage_by_model": usage,
                            "input_count": _count_payload(input_data), "output_count": _count_payload(encoded)},
            }
            snapshots.append(snapshot)
            previous = encoded
            run.snapshots = snapshots
            run.cascade_state = snapshot["state"]
            run.current_step = step
            run.total_seconds = run.total_seconds + seconds
            run.total_cost_rub = round(sum(item["metrics"]["cost_rub"] for item in snapshots), 4)
            run.status = "completed" if step == 8 else "running"
            run.save()
            cost_limit = float(run.settings.get("max_cost_rub", 0) or 0)
            time_limit = float(run.settings.get("max_seconds", 0) or 0)
            if step < run.stop_after and cost_limit and run.total_cost_rub >= cost_limit:
                pause_reason = f"Достигнут лимит {cost_limit:g} ₽"
                break
            if step < run.stop_after and time_limit and run.total_seconds >= time_limit:
                pause_reason = f"Достигнут лимит {time_limit:g} секунд"
                break
        run.status = "completed" if run.current_step == 8 else "paused"
        if pause_reason:
            run.result = {"pause_reason": pause_reason}
        if run.current_step == 8:
            run.result = evaluate_expectations(
                _decode_output(previous), run.expectations,
                total_seconds=run.total_seconds, total_cost_rub=run.total_cost_rub,
            )
        run.save()
    except Exception as exc:
        run.status, run.error = "error", str(exc)[:1000]
        run.save(update_fields=["status", "error", "updated_at"])
        raise
    finally:
        close_old_connections()


def _count_payload(value):
    if isinstance(value, dict) and "count" in value:
        return value["count"]
    if isinstance(value, (list, dict)):
        return len(value)
    return 1 if value is not None else 0
