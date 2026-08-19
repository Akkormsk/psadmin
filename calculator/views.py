import json
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import CalculatorSettings, Estimate, EstimateLine, PriceItem
from .services import calculate_sheet_estimate


def _settings():
    return CalculatorSettings.objects.get_or_create(pk=1)[0]


def _estimate_for_user(request, pk):
    estimate = get_object_or_404(Estimate, pk=pk)
    if not request.user.is_superuser and estimate.owner_id != request.user.id:
        return None
    return estimate


@login_required
def home(request, pk=None):
    estimate = _estimate_for_user(request, pk) if pk else None
    settings = _settings()
    calculator_type = estimate.calculator_type if estimate else request.GET.get("calculator", Estimate.TYPE_SHEET)
    if calculator_type not in {Estimate.TYPE_SHEET, Estimate.TYPE_WIDE}:
        calculator_type = Estimate.TYPE_SHEET
    if request.method == "POST":
        try:
            calculator_type = request.POST.get("calculator_type", Estimate.TYPE_SHEET)
            if calculator_type not in {Estimate.TYPE_SHEET, Estimate.TYPE_WIDE}:
                raise ValueError
            raw_lines = json.loads(request.POST.get("lines_json", "[]"))
            product_quantity = max(1, int(request.POST.get("product_quantity", 1)))
            work_hours = Decimal(request.POST.get("work_hours", "0"))
            if work_hours < 0 or (work_hours * 2) % 1:
                raise ValueError
        except (ValueError, InvalidOperation, json.JSONDecodeError):
            messages.error(request, "Проверьте тираж, часы и добавленные строки.")
        else:
            lines = []
            item_ids = [row.get("item_id") for row in raw_lines if row.get("item_id")]
            items = PriceItem.objects.in_bulk(item_ids)
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
                except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
                    messages.error(request, "Одна из строк расчёта заполнена неверно.")
                    break
            else:
                estimate = estimate or Estimate(owner=request.user)
                estimate.name = request.POST.get("name", "").strip() or "Новый расчёт"
                estimate.calculator_type = calculator_type
                estimate.product_quantity = product_quantity
                estimate.work_hours = work_hours
                estimate.settings_snapshot = {"hourly_rate": str(settings.hourly_rate), "material_coefficient": str(settings.material_coefficient), "time_coefficient": str(settings.time_coefficient)}
                summary = calculate_sheet_estimate(lines, product_quantity, work_hours, settings, calculator_type)
                estimate.summary_snapshot = {key: str(value) for key, value in summary.items()}
                estimate.save()
                estimate.lines.all().delete()
                EstimateLine.objects.bulk_create([
                    EstimateLine(estimate=estimate, category=line["category"], price_item=line["item"], name_snapshot=line["name"], unit_price_snapshot=line["unit_price"], quantity=line["quantity"], is_custom=line["custom"])
                    for line in lines
                ])
                messages.success(request, "Расчёт сохранён.")
                return redirect("calculator_estimate", pk=estimate.pk)

    initial_lines = []
    if estimate:
        initial_lines = [{"category": line.category, "item_id": line.price_item_id, "name": line.name_snapshot, "unit_price": str(line.unit_price_snapshot), "quantity": str(line.quantity), "custom": line.is_custom} for line in estimate.lines.select_related("price_item")]
    items = [{"id": item.pk, "category": item.category, "name": item.name, "unit_name": item.unit_name, "unit_price": str(item.effective_unit_price)} for item in PriceItem.objects.filter(is_active=True).select_related("base_item")]
    estimates = Estimate.objects.filter(owner=request.user) if not request.user.is_superuser else Estimate.objects.all()
    estimates = estimates.filter(calculator_type=calculator_type).select_related("owner", "owner__profile").defer("owner__profile__avatar_data")
    return render(request, "calculator/sheet.html", {"settings": settings, "calculator_type": calculator_type, "items_json": json.dumps(items, ensure_ascii=False), "initial_lines_json": json.dumps(initial_lines, ensure_ascii=False), "estimate": estimate, "estimates": estimates[:20]})


@login_required
@require_POST
def delete_estimate(request, pk):
    estimate = _estimate_for_user(request, pk)
    if estimate is None:
        raise Http404
    estimate.delete()
    messages.success(request, "Сохранённый расчёт удалён.")
    return redirect("calculator_home")
