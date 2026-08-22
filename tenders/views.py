import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from openpyxl import load_workbook

from .models import TenderEstimate, TenderLine, TenderSettings
from .services import TenderAIError, calculate_tender, recognize_tender_items


def _estimate_for_user(request, pk):
    estimate = get_object_or_404(TenderEstimate, pk=pk)
    if not request.user.is_superuser and estimate.owner_id != request.user.id:
        raise Http404
    return estimate


def _number(value, default="0"):
    return Decimal(str(value or default).replace(",", "."))


@login_required
@require_POST
def import_preview(request):
    upload = request.FILES.get("file")
    if upload is None or not upload.name.lower().endswith(".xlsx"):
        return JsonResponse({"error": "Выберите файл Excel в формате .xlsx."}, status=400)
    if upload.size > 10 * 1024 * 1024:
        return JsonResponse({"error": "Файл больше 10 МБ."}, status=400)
    try:
        workbook = load_workbook(upload, read_only=True, data_only=True)
        sheet_name = request.POST.get("sheet")
        sheet = workbook[sheet_name] if sheet_name in workbook.sheetnames else workbook[workbook.sheetnames[0]]
        max_columns = min(sheet.max_column or 1, 50)
        max_rows = min(sheet.max_row or 1, 500)
        rows = []
        for values in sheet.iter_rows(min_row=1, max_row=max_rows, max_col=max_columns, values_only=True):
            rows.append(["" if value is None else str(value) for value in values])
        while rows and not any(value.strip() for value in rows[-1]):
            rows.pop()
        return JsonResponse({"sheets": workbook.sheetnames, "sheet": sheet.title, "rows": rows, "truncated": (sheet.max_row or 0) > max_rows})
    except Exception:
        return JsonResponse({"error": "Не удалось прочитать файл. Проверьте, что это корректный .xlsx."}, status=400)


@login_required
@require_POST
def ai_import_preview(request):
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"error": "Выберите документ."}, status=400)
    if upload.size > 10 * 1024 * 1024:
        return JsonResponse({"error": "Файл больше 10 МБ."}, status=400)
    if Path(upload.name).suffix.lower() not in {".xlsx", ".xls", ".docx", ".pdf"}:
        return JsonResponse({"error": "Поддерживаются .xlsx, .xls, .docx и текстовые .pdf."}, status=400)
    try:
        return JsonResponse(recognize_tender_items(upload))
    except TenderAIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"error": "Не удалось прочитать документ. Проверьте файл и попробуйте ещё раз."}, status=400)


@login_required
def home(request, pk=None):
    estimate = _estimate_for_user(request, pk) if pk else None
    settings = TenderSettings.objects.get_or_create(pk=1)[0]
    posted_lines = None
    form_state = {
        "tender_number": estimate.tender_number if estimate else "",
        "name": estimate.name if estimate else "",
        "reduction_percent": estimate.reduction_percent if estimate else Decimal("30.00"),
        "russia_delivery": estimate.russia_delivery if estimate else Decimal("0.00"),
        "owner_id": estimate.owner_id if estimate else request.user.id,
    }
    if request.method == "POST":
        form_state = {
            "tender_number": request.POST.get("tender_number", ""),
            "name": request.POST.get("name", ""),
            "reduction_percent": request.POST.get("reduction_percent", "30"),
            "russia_delivery": request.POST.get("russia_delivery", "0"),
            "owner_id": request.POST.get("owner_id") or request.user.id,
        }
        try:
            tender_number = request.POST.get("tender_number", "").strip()
            name = request.POST.get("name", "").strip()
            reduction_percent = _number(request.POST.get("reduction_percent"), "30")
            russia_delivery = _number(request.POST.get("russia_delivery"))
            raw_lines = json.loads(request.POST.get("lines_json", "[]"))
            posted_lines = raw_lines if isinstance(raw_lines, list) else []
            if not tender_number or not name or not Decimal("0") <= reduction_percent <= Decimal("100") or russia_delivery < 0:
                raise ValueError
            lines = []
            for row in raw_lines:
                line = {
                    "name": str(row.get("name", "")).strip(),
                    "quantity": _number(row.get("quantity")),
                    "nmck_unit": _number(row.get("nmck_unit")),
                    "material_unit": _number(row.get("material_unit")),
                    "application_unit": _number(row.get("application_unit")),
                    "logistics_unit": _number(row.get("logistics_unit")),
                    "product_url": str(row.get("product_url", "")).strip(),
                    "comment": str(row.get("comment", "")).strip(),
                }
                has_expense = any(line[key] > 0 for key in ("material_unit", "application_unit", "logistics_unit"))
                if not line["name"] or line["quantity"] <= 0 or line["nmck_unit"] <= 0 or not has_expense or min(line["material_unit"], line["application_unit"], line["logistics_unit"]) < 0:
                    raise ValueError
                lines.append(line)
            if not lines:
                raise ValueError
        except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError):
            messages.error(request, "Проверьте реквизиты тендера и товарные позиции.")
        else:
            calculated, summary = calculate_tender(lines, reduction_percent, russia_delivery, settings.vat_rate)
            estimate = estimate or TenderEstimate(owner=request.user)
            if request.user.is_superuser and request.POST.get("owner_id"):
                estimate.owner = get_object_or_404(get_user_model(), pk=request.POST["owner_id"])
            estimate.tender_number = tender_number[:100]
            estimate.name = name[:300]
            estimate.reduction_percent = reduction_percent
            estimate.russia_delivery = russia_delivery
            estimate.vat_rate_snapshot = settings.vat_rate
            estimate.summary_snapshot = {key: str(value) for key, value in summary.items()}
            estimate.save()
            estimate.lines.all().delete()
            TenderLine.objects.bulk_create([TenderLine(estimate=estimate, sort_order=index, **line) for index, line in enumerate(lines)])
            messages.success(request, "Просчёт тендера сохранён.")
            return redirect("tender_estimate", pk=estimate.pk)

    initial_lines = []
    if posted_lines is not None:
        initial_lines = posted_lines
    elif estimate:
        initial_lines = [{"name": line.name, "quantity": str(line.quantity), "nmck_unit": str(line.nmck_unit), "material_unit": str(line.material_unit), "application_unit": str(line.application_unit), "logistics_unit": str(line.logistics_unit), "product_url": line.product_url, "comment": line.comment} for line in estimate.lines.all()]
    estimates = TenderEstimate.objects.all() if request.user.is_superuser else TenderEstimate.objects.filter(owner=request.user)
    users = get_user_model().objects.filter(is_active=True).order_by("last_name", "first_name", "username") if request.user.is_superuser else None
    return render(request, "tenders/home.html", {"estimate": estimate, "form_state": form_state, "estimates": estimates.select_related("owner", "owner__profile")[:30], "initial_lines_json": json.dumps(initial_lines, ensure_ascii=False), "vat_rate": settings.vat_rate, "users": users})


@login_required
@require_POST
def delete_estimate(request, pk):
    estimate = _estimate_for_user(request, pk)
    estimate.delete()
    messages.success(request, "Просчёт тендера удалён.")
    return redirect("tender_home")
