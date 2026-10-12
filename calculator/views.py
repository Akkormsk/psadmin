import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import CalculatorSettings, Estimate, EstimateGroup, PriceItem
from .actions import CalculatorActionError, delete_estimate as delete_saved_estimate, duplicate_estimate as duplicate_saved_estimate, save_estimate as save_shared_estimate


def _settings():
    return CalculatorSettings.objects.get_or_create(pk=1)[0]


def _estimate_for_user(request, pk):
    estimate = get_object_or_404(Estimate, pk=pk)
    if not request.user.is_superuser and estimate.owner_id != request.user.id:
        return None
    return estimate


def _persist_estimate(request, estimate):
    """Parse the posted calculator state and write it to ``estimate``
    (creating one when it is ``None``). Returns ``(estimate, error)`` —
    ``error`` is a message string when nothing was saved. Shared by the
    plain form POST and the autosave endpoint."""
    values = {
        "calculator_type": request.POST.get("calculator_type", Estimate.TYPE_SHEET),
        "lines": request.POST.get("lines_json", "[]"),
        "product_quantity": request.POST.get("product_quantity", 1),
        "work_hours": request.POST.get("work_hours", "0"),
        "name": request.POST.get("name", ""),
        "comment": request.POST.get("comment", ""),
    }
    try:
        result = save_shared_estimate(request.user, values, estimate.pk if estimate else None)
    except CalculatorActionError as exc:
        return estimate, str(exc)
    return Estimate.objects.get(pk=result["estimate_id"]), None


@login_required
def home(request, pk=None):
    estimate = _estimate_for_user(request, pk) if pk else None
    settings = _settings()
    calculator_type = estimate.calculator_type if estimate else request.GET.get("calculator", Estimate.TYPE_SHEET)
    if calculator_type not in {Estimate.TYPE_SHEET, Estimate.TYPE_WIDE}:
        calculator_type = Estimate.TYPE_SHEET
    if request.method == "POST":
        posted_type = request.POST.get("calculator_type")
        if posted_type in {Estimate.TYPE_SHEET, Estimate.TYPE_WIDE}:
            calculator_type = posted_type
        estimate, error = _persist_estimate(request, estimate)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, "Расчёт сохранён.")
            return redirect("calculator_estimate", pk=estimate.pk)

    initial_lines = []
    if estimate:
        initial_lines = [{"category": line.category, "item_id": line.price_item_id, "name": line.name_snapshot, "unit_price": str(line.unit_price_snapshot), "quantity": str(line.quantity), "custom": line.is_custom} for line in estimate.lines.select_related("price_item")]
    items = [{"id": item.pk, "category": item.category, "name": item.name, "unit_name": item.unit_name, "unit_price": str(item.effective_unit_price)} for item in PriceItem.objects.filter(is_active=True).select_related("base_item")]
    estimates = Estimate.objects.filter(owner=request.user) if not request.user.is_superuser else Estimate.objects.all()
    estimates = list(estimates.filter(calculator_type=calculator_type).select_related("owner", "owner__profile", "group").defer("owner__profile__avatar_data"))
    grouped = {}
    ungrouped = []
    for saved in estimates:
        (grouped.setdefault(saved.group, []) if saved.group else ungrouped).append(saved)
    estimate_sections = []
    for group, grouped_estimates in grouped.items():
        total = sum((Decimal(str(item.summary_snapshot.get("standard", 0))) for item in grouped_estimates), Decimal("0"))
        estimate_sections.append({"group": group, "estimates": grouped_estimates, "total": total})
    if ungrouped:
        estimate_sections.append({"group": None, "estimates": ungrouped, "total": None})
    return render(request, "calculator/sheet.html", {"settings": settings, "calculator_type": calculator_type, "items_json": json.dumps(items, ensure_ascii=False), "initial_lines_json": json.dumps(initial_lines, ensure_ascii=False), "estimate": estimate, "estimate_sections": estimate_sections})


@login_required
@require_POST
def save_estimate(request, pk=None):
    """Autosave: same write as the form POST, but returns JSON and never
    redirects — the calculator page keeps its state."""
    estimate = None
    if pk:
        estimate = _estimate_for_user(request, pk)
        if estimate is None:
            raise Http404
    estimate, error = _persist_estimate(request, estimate)
    if error:
        return JsonResponse({"error": error}, status=400)
    return JsonResponse({
        "pk": estimate.pk,
        "url": reverse("calculator_estimate", args=[estimate.pk]),
        "saved_at": timezone.localtime(estimate.updated_at).strftime("%H:%M"),
        "standard": estimate.summary_snapshot.get("standard", ""),
    })


@login_required
@require_POST
def duplicate_estimate(request, pk):
    try:
        copy = duplicate_saved_estimate(request.user, pk)
    except CalculatorActionError:
        raise Http404
    messages.success(request, f"Создана копия расчёта «{copy['name']}».")
    return redirect("calculator_estimate", pk=copy["estimate_id"])


@login_required
@require_POST
def delete_estimate(request, pk):
    try:
        delete_saved_estimate(request.user, pk)
    except CalculatorActionError:
        raise Http404
    messages.success(request, "Сохранённый расчёт удалён.")
    return redirect("calculator_home")


@login_required
@require_POST
def bulk_estimates(request):
    ids = [int(value) for value in request.POST.getlist("estimate_ids") if value.isdigit()]
    estimates = Estimate.objects.filter(pk__in=ids)
    if not request.user.is_superuser:
        estimates = estimates.filter(owner=request.user)
    estimates = list(estimates)
    action = request.POST.get("action")
    if not estimates:
        messages.error(request, "Выберите хотя бы один расчёт.")
    elif action == "group":
        name = request.POST.get("name", "").strip()[:200]
        if not name:
            messages.error(request, "Укажите название группы.")
        elif len({estimate.calculator_type for estimate in estimates}) != 1:
            messages.error(request, "В одну группу можно собрать расчёты одного калькулятора.")
        else:
            group, _ = EstimateGroup.objects.get_or_create(owner=request.user, calculator_type=estimates[0].calculator_type, name=name)
            Estimate.objects.filter(pk__in=[estimate.pk for estimate in estimates]).update(group=group)
            messages.success(request, f"Расчёты собраны в группу «{group.name}».")
    elif action == "ungroup":
        Estimate.objects.filter(pk__in=[estimate.pk for estimate in estimates]).update(group=None)
        messages.success(request, "Расчёты убраны из группы.")
    elif action == "delete":
        Estimate.objects.filter(pk__in=[estimate.pk for estimate in estimates]).delete()
        messages.success(request, "Выбранные расчёты удалены.")
    else:
        messages.error(request, "Неизвестное массовое действие.")
    return redirect(f"{reverse('calculator_home')}?calculator={request.POST.get('calculator_type', Estimate.TYPE_SHEET)}")
