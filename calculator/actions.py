"""Business actions shared by calculator views and assistant adapters."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from .models import CalculatorSettings, Estimate, EstimateGroup, EstimateLine, PriceItem
from .services import calculate_sheet_estimate


class CalculatorActionError(Exception):
    pass


def _calculation_values(values):
    try:
        calculator_type = values.get("calculator_type", Estimate.TYPE_SHEET)
        if calculator_type not in {Estimate.TYPE_SHEET, Estimate.TYPE_WIDE}:
            raise ValueError
        raw_lines = values.get("lines", [])
        if isinstance(raw_lines, str):
            raw_lines = json.loads(raw_lines)
        product_quantity = max(1, int(values.get("product_quantity", 1)))
        work_hours = Decimal(str(values.get("work_hours", "0")))
        if not isinstance(raw_lines, list) or work_hours < 0 or (work_hours * 2) % 1:
            raise ValueError
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
        raise CalculatorActionError("Проверьте тираж, часы и добавленные строки.") from exc
    items = PriceItem.objects.in_bulk([row.get("item_id") for row in raw_lines if isinstance(row, dict) and row.get("item_id")])
    lines = []
    for row in raw_lines:
        try:
            quantity = Decimal(str(row.get("quantity", "0")))
            category = row["category"]
            if quantity <= 0:
                continue
            item = items.get(int(row["item_id"])) if row.get("item_id") else None
            custom = bool(row.get("custom"))
            price = Decimal(str(row.get("unit_price"))) if custom else item.effective_unit_price
            name = str(row.get("name", "")).strip() if custom else item.name
            if not name or price < 0:
                raise ValueError
            lines.append({"category": category, "quantity": quantity, "unit_price": price, "name": name, "item": item, "custom": custom})
        except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError) as exc:
            raise CalculatorActionError("Одна из строк расчёта заполнена неверно.") from exc
    return calculator_type, product_quantity, work_hours, lines


def calculate_estimate(values):
    calculator_type, product_quantity, work_hours, lines = _calculation_values(values)
    settings = CalculatorSettings.objects.get_or_create(pk=1)[0]
    summary = calculate_sheet_estimate(lines, product_quantity, work_hours, settings, calculator_type)
    return calculator_type, product_quantity, work_hours, lines, settings, summary


def save_estimate(actor, values, estimate_id=None):
    calculator_type, product_quantity, work_hours, lines, settings, summary = calculate_estimate(values)
    estimate = estimate_for_user(actor, estimate_id) if estimate_id else Estimate(owner=actor)
    estimate.name = str(values.get("name", "")).strip() or "Новый расчёт"
    estimate.comment = str(values.get("comment", "")).strip()[:300]
    estimate.calculator_type = calculator_type
    estimate.product_quantity = product_quantity
    estimate.work_hours = work_hours
    estimate.settings_snapshot = {"hourly_rate": str(settings.hourly_rate), "material_coefficient": str(settings.material_coefficient), "time_coefficient": str(settings.time_coefficient)}
    estimate.summary_snapshot = {key: str(value) for key, value in summary.items()}
    estimate.save()
    estimate.lines.all().delete()
    EstimateLine.objects.bulk_create([
        EstimateLine(estimate=estimate, category=line["category"], price_item=line["item"], name_snapshot=line["name"], unit_price_snapshot=line["unit_price"], quantity=line["quantity"], is_custom=line["custom"])
        for line in lines
    ])
    return {"estimate_id": estimate.pk, "name": estimate.name, "calculator_type": calculator_type, "summary": estimate.summary_snapshot}


def estimate_for_user(actor, estimate_id):
    estimate = Estimate.objects.filter(pk=estimate_id).first()
    if estimate is None or (not actor.is_superuser and estimate.owner_id != actor.pk):
        raise CalculatorActionError("Расчёт не найден или недоступен")
    return estimate


def duplicate_estimate(actor, estimate_id):
    original = estimate_for_user(actor, estimate_id)
    lines = list(original.lines.all())
    copy = Estimate.objects.create(
        owner=actor,
        calculator_type=original.calculator_type,
        name=f"{original.name} (копия)"[:200],
        comment=original.comment,
        product_quantity=original.product_quantity,
        work_hours=original.work_hours,
        settings_snapshot=original.settings_snapshot,
        summary_snapshot=original.summary_snapshot,
    )
    EstimateLine.objects.bulk_create([
        EstimateLine(
            estimate=copy, category=line.category, price_item=line.price_item,
            name_snapshot=line.name_snapshot, unit_name=line.unit_name,
            unit_price_snapshot=line.unit_price_snapshot, quantity=line.quantity,
            is_custom=line.is_custom,
        ) for line in lines
    ])
    return {"estimate_id": copy.pk, "source_estimate_id": original.pk, "name": copy.name}


def delete_estimate(actor, estimate_id):
    estimate = estimate_for_user(actor, estimate_id)
    estimate.delete()
    return {"estimate_id": estimate_id, "deleted": True}


def bulk_estimates(actor, values):
    if not isinstance(values, dict):
        raise CalculatorActionError("Некорректные параметры массового действия")
    action = values.get("action")
    estimate_ids = values.get("estimate_ids")
    if action not in {"group", "ungroup", "delete"} or not isinstance(estimate_ids, list):
        raise CalculatorActionError("Неизвестное массовое действие")
    ids = [value for value in estimate_ids if isinstance(value, int) and not isinstance(value, bool)]
    estimates = Estimate.objects.filter(pk__in=ids)
    if not actor.is_superuser:
        estimates = estimates.filter(owner=actor)
    estimates = list(estimates)
    if not estimates:
        raise CalculatorActionError("Выберите хотя бы один расчёт")
    if action == "group":
        name = str(values.get("name", "")).strip()[:200]
        if not name:
            raise CalculatorActionError("Укажите название группы")
        if len({estimate.calculator_type for estimate in estimates}) != 1:
            raise CalculatorActionError("В одну группу можно собрать расчёты одного калькулятора")
        group, _ = EstimateGroup.objects.get_or_create(
            owner=actor, calculator_type=estimates[0].calculator_type, name=name,
        )
        Estimate.objects.filter(pk__in=[estimate.pk for estimate in estimates]).update(group=group)
        return {"action": action, "group_id": group.pk, "changed": len(estimates)}
    if action == "ungroup":
        Estimate.objects.filter(pk__in=[estimate.pk for estimate in estimates]).update(group=None)
        return {"action": action, "changed": len(estimates)}
    deleted = [estimate.pk for estimate in estimates]
    Estimate.objects.filter(pk__in=deleted).delete()
    return {"action": action, "deleted_ids": deleted, "changed": len(deleted)}
