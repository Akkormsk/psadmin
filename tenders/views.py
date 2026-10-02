import atexit
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import serializers
from django.db import close_old_connections, transaction
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from openpyxl import load_workbook

from .models import CascadeConfigVersion, CascadeLabPreset, CatalogCategory, CatalogMatchDecision, CatalogProduct, CatalogSyncRun, CatalogSupplier, Counterparty, Lesson, Order, OrderEstimate, OrderLine, ProcessDefinition, ProductionTrainingExample, ProductionTrainingSession, ProductionTrainingTurn, ProductionType, Proposal, RequirementSkipRule, StageCounterpartyLink, TenderEstimate, TenderKnowledgeSource, TenderLine, TenderSettings
from .proposals import apply_batch, apply_proposal, payload_from_feedback_item
from .knowledge import export_knowledge_bundle
from .cascade_lab import execute_cascade_steps
from .cascade_settings import text_search_settings
from .catalog import CatalogSyncError, GiftsXmlClient, _gifts_text, sync_gifts_catalog, sync_gifts_categories
from .services import TenderAIError, _normalized_text as _normalized_requirement_label, _resolve_line_match, analyze_tender_requirements, apply_catalog_candidate, apply_verified_source_quote, build_training_hypothesis, calculate_tender, detect_tender_document_type, extract_calculation_source, inspect_tender_document, learn_lessons_from_session, parse_counterparty_draft, parse_production_feedback, recognize_tender_items, refresh_training_example_embedding


logger = logging.getLogger(__name__)


SUPPORTED_TENDER_DOCUMENTS = {".xlsx", ".xls", ".doc", ".docx", ".pdf"}


# A tender worker process must never spawn an unbounded number of hypothesis
# threads: each one holds a full supplier catalogue in memory, and repeated
# "Повторить" clicks used to exhaust RAM. All assistant jobs share one small pool,
# and a session already being processed is never queued twice.
_ASSISTANT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="assistant-job")
_ASSISTANT_INFLIGHT = set()
_ASSISTANT_INFLIGHT_LOCK = threading.Lock()
atexit.register(_ASSISTANT_EXECUTOR.shutdown, wait=False)

_STAGE_LABELS = {
    "route": "Проектирую маршрут и учитываю ваши замечания…",
    "cases": "Готовлю поиск…",
    "ai": "Убираю лишние слова из названия и подбираю запросы…",
    "catalog": "Ищу товары поставщиков по названию…",
    "shortlist": "Сверяю характеристики по ТЗ и применяю ваши замечания…",
    "finalizing": "Формирую результат…",
}


def _record_stage(session_id, stage):
    ProductionTrainingSession.objects.filter(pk=session_id).update(current_hypothesis={
        "status": "processing", "stage": stage,
        "stage_label": _STAGE_LABELS.get(stage, "Идёт расчёт…"),
    })


def _run_assistant_job(session_id, work, fallback):
    close_old_connections()
    try:
        session = ProductionTrainingSession.objects.get(pk=session_id)
        hypothesis = work(session)
        hypothesis["session_id"] = session_id
        session.current_hypothesis = hypothesis
        session.save()
    except TenderAIError as exc:
        ProductionTrainingSession.objects.filter(pk=session_id).update(
            current_hypothesis={**fallback, "status": "error", "error": str(exc)}
        )
    except Exception:
        logger.exception("Unexpected background assistant job error")
        ProductionTrainingSession.objects.filter(pk=session_id).update(
            current_hypothesis={**fallback, "status": "error", "error": "Не удалось построить расчёт. Подробная причина записана в журнал приложения."}
        )
    finally:
        with _ASSISTANT_INFLIGHT_LOCK:
            _ASSISTANT_INFLIGHT.discard(session_id)
        close_old_connections()


def _submit_assistant_job(session_id, work, fallback=None):
    """Queue work(session) unless that session is already being processed.

    ``fallback`` is the hypothesis to keep (marked as errored) if the job fails,
    so a failed revision does not wipe a good previous result.
    """
    with _ASSISTANT_INFLIGHT_LOCK:
        if session_id in _ASSISTANT_INFLIGHT:
            return
        _ASSISTANT_INFLIGHT.add(session_id)
    _ASSISTANT_EXECUTOR.submit(_run_assistant_job, session_id, work, fallback or {})


def _cascade_lab_allowed(request):
    return request.user.is_authenticated and request.user.is_superuser


@login_required
@require_GET
def cascade_lab(request):
    if not _cascade_lab_allowed(request):
        return HttpResponse(status=403)
    from .cascade_lab import STEP_DEFINITIONS
    from .gateway_budget import model_catalog

    selected_line = None
    try:
        selected_line_id = int(request.GET.get("line_id") or 0)
    except (TypeError, ValueError):
        selected_line_id = 0
    if selected_line_id:
        selected_line = TenderLine.objects.select_related("estimate").filter(pk=selected_line_id).first()
    active_config = CascadeConfigVersion.objects.filter(is_active=True).first()
    return render(request, "tenders/cascade_lab.html", {
        "steps": STEP_DEFINITIONS,
        "lines": TenderLine.objects.select_related("estimate").order_by("-estimate__updated_at", "sort_order")[:250],
        "selected_line": selected_line,
        "lab_presets": [
            {"id": preset.pk, "name": preset.name, "settings_json": json.dumps(text_search_settings(preset.settings), ensure_ascii=False)}
            for preset in CascadeLabPreset.objects.filter(created_by=request.user)[:100]
        ],
        "active_config": active_config,
        # Открыв лабораторию, должны видеть то, что реально сейчас в поиске —
        # не захардкоженные дефолты формы. JS сразу применяет эти значения.
        "active_config_settings_json": json.dumps(text_search_settings(active_config.settings), ensure_ascii=False) if active_config else "",
        # Список моделей не зашит в код — тянется у самого шлюза (кэш 6
        # часов), поэтому здесь ровно то, что реально можно выбрать. Название,
        # тариф и контекст — из MODEL_LABELS/RATES_RUB_PER_M (шлюз цену по API
        # не отдаёт) плюс max_output_tokens, когда шлюз его прислал сам.
        "model_options": model_catalog(),
    })


def _lab_json(value, default):
    if not str(value or "").strip():
        return default
    parsed = json.loads(value)
    if not isinstance(parsed, type(default)):
        raise ValueError
    return parsed


def _line_payload(line):
    return {
        "name": line.name, "quantity": str(line.quantity), "nmck_unit": str(line.nmck_unit),
        "requirements": line.requirements if isinstance(line.requirements, dict) else {},
    }


def _lab_line_from_fields(request):
    requirements = [
        {"label": label.strip(), "value": value.strip()}
        for label, value in zip(
            request.POST.getlist("requirement_label"),
            request.POST.getlist("requirement_value"),
        )
        if label.strip() and value.strip()
    ]
    return {
        "name": str(request.POST.get("line_name") or "").strip(),
        "quantity": str(request.POST.get("line_quantity") or "").strip(),
        "requirements": {"requirements": requirements},
    }


def _lab_step_settings(request, current=None):
    steps = {
        str(step): dict(values)
        for step, values in (current or {}).items()
        if isinstance(values, dict)
    }
    fields = {
        "step_1_max_requirements": ("1", "max_active_requirements", 0, 100),
        "step_2_min_phrases": ("2", "min_phrases", 1, 40),
        "step_2_max_phrases": ("2", "max_phrases", 1, 40),
        "step_5_tolerance_percent": ("5", "tolerance_percent", 0, 50),
    }
    for field, (step, key, minimum, maximum) in fields.items():
        raw = str(request.POST.get(field) or "").strip()
        if raw:
            steps.setdefault(step, {})[key] = max(minimum, min(maximum, int(raw)))
    from .gateway_budget import available_models

    agents = set(available_models()) | {"strong", "fast"}
    choices = {
        "step_1_model": ("1", "model", agents),
        "step_1_cache": ("1", "cache", {"yes", "no"}),
        "step_2_model": ("2", "model", agents),
        "step_2_cache": ("2", "cache", {"yes", "no"}),
        "step_3_sources": ("3", "sources", {"all", "oasis", "gifts"}),
        "step_4_model": ("4", "model", agents),
        "step_4_intensity": ("4", "intensity", {"off", "cautious", "strict"}),
        "step_4_cache": ("4", "cache", {"yes", "no"}),
        "step_5_color_filter": ("5", "color_filter", {"family", "off"}),
        "step_5_stock_policy": ("5", "stock_policy", {"available", "enough", "ignore"}),
        "step_5_numeric_prefill": ("5", "numeric_prefill", {"yes", "no"}),
        "step_6_engine": ("6", "engine", {"llm", "jev"}),
        "step_6_model": ("6", "model", agents),
        "step_6_cache": ("6", "cache", {"yes", "no"}),
        "step_7_matrix_order": ("7", "matrix_order", {"no_then_yes", "yes_then_no"}),
        "step_7_price_order": ("7", "price_order", {"asc", "desc"}),
        "step_8_live_prices": ("8", "live_prices", {"yes", "no"}),
    }
    for field, (step, key, allowed) in choices.items():
        value = str(request.POST.get(field) or "").strip()
        if value in allowed:
            steps.setdefault(step, {})[key] = value
    return steps


def _lab_request_payload(request):
    line_id = int(request.POST.get("line_id") or 0)
    if line_id:
        line = _line_payload(TenderLine.objects.get(pk=line_id))
    elif str(request.POST.get("line_json") or "").strip():
        line = _lab_json(request.POST.get("line_json"), {})
    else:
        line = _lab_line_from_fields(request)
    settings = text_search_settings(_lab_json(request.POST.get("settings"), {"steps": {}}))
    if "settings" not in request.POST:
        settings["steps"] = _lab_step_settings(request, settings.get("steps"))
        settings.update({
            "custom_cards": _lab_json(request.POST.get("cards_json"), []),
            "top": max(1, min(50, int(request.POST.get("step_8_top") or 10))),
            "max_cost_rub": max(0, float(request.POST.get("max_cost_rub") or 10)),
            "max_seconds": max(0, float(request.POST.get("max_seconds") or 10)),
        })
    if not str(line.get("name") or "").strip():
        raise ValueError
    return line, settings


@login_required
@require_POST
def cascade_lab_execute(request):
    if not _cascade_lab_allowed(request):
        return JsonResponse({"error": "Лаборатория доступна только администратору."}, status=403)
    try:
        line, settings = _lab_request_payload(request)
        result = execute_cascade_steps(
            line=line,
            settings=settings,
            from_step=max(1, min(8, int(request.POST.get("from_step") or 1))),
            stop_after=max(1, min(8, int(request.POST.get("stop_after") or 8))),
            snapshots=_lab_json(request.POST.get("snapshots"), []),
            cascade_state=_lab_json(request.POST.get("cascade_state"), {}),
            expectations=_lab_json(request.POST.get("expectations_json"), {}),
            prior_total_seconds=float(request.POST["prior_total_seconds"]) if request.POST.get("prior_total_seconds") else None,
            prior_total_cost_rub=float(request.POST["prior_total_cost_rub"]) if request.POST.get("prior_total_cost_rub") else None,
        )
        return JsonResponse(result, json_dumps_params={"ensure_ascii": False})
    except TimeoutError as exc:
        return JsonResponse({"error": str(exc)}, status=408)
    except (ValueError, TypeError, json.JSONDecodeError, TenderLine.DoesNotExist) as exc:
        return JsonResponse({"error": str(exc) or "Проверьте входные данные."}, status=400)


@login_required
@require_POST
def cascade_lab_preset_save(request):
    if not _cascade_lab_allowed(request):
        return JsonResponse({"error": "Лаборатория доступна только администратору."}, status=403)
    try:
        name = str(request.POST.get("name") or "").strip()[:200]
        settings = text_search_settings(_lab_json(request.POST.get("settings"), {}))
        if not name:
            raise ValueError("Введите название набора настроек.")
        preset, created = CascadeLabPreset.objects.update_or_create(
            created_by=request.user, name=name, defaults={"settings": settings},
        )
        return JsonResponse({"id": preset.pk, "name": preset.name, "settings": preset.settings, "created": created})
    except (ValueError, json.JSONDecodeError) as exc:
        return JsonResponse({"error": str(exc) or "Некорректные настройки."}, status=400)


@login_required
@require_POST
def cascade_lab_activate(request):
    if not _cascade_lab_allowed(request):
        return JsonResponse({"error": "Лаборатория доступна только администратору."}, status=403)
    try:
        name = str(request.POST.get("name") or "Текущие настройки").strip()[:200]
        settings = text_search_settings(_lab_json(request.POST.get("settings"), {}))
        with transaction.atomic():
            CascadeConfigVersion.objects.filter(is_active=True).update(is_active=False)
            version = CascadeConfigVersion.objects.create(
                name=name, settings=settings, created_by=request.user, is_active=True,
            )
        return JsonResponse({"id": version.pk, "name": version.name, "created_at": version.created_at.isoformat()})
    except (ValueError, json.JSONDecodeError) as exc:
        return JsonResponse({"error": str(exc) or "Некорректные настройки."}, status=400)


def knowledge_sync(request):
    expected = os.getenv("KNOWLEDGE_SYNC_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
        return HttpResponse(status=403)
    return JsonResponse(export_knowledge_bundle(include_embeddings=True), json_dumps_params={"ensure_ascii": False})


@require_GET
def catalog_sync(request):
    expected = os.getenv("KNOWLEDGE_SYNC_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
        return HttpResponse(status=403)

    def dump():
        querysets = (
            CatalogSupplier.objects.all(),
            CatalogCategory.objects.order_by("pk").iterator(),
            CatalogProduct.objects.order_by("pk").iterator(),
        )
        yield "["
        first = True
        for queryset in querysets:
            for instance in queryset:
                fragment = serializers.serialize("json", [instance])[1:-1]
                yield fragment if first else "," + fragment
                first = False
        yield "]"

    return StreamingHttpResponse(dump(), content_type="application/json")


@require_GET
def gifts_import_test(request):
    expected = os.getenv("KNOWLEDGE_SYNC_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
        return HttpResponse(status=403)
    if request.GET.get("status") == "1":
        run = CatalogSyncRun.objects.filter(supplier__code="gifts").first()
        if run is None:
            return JsonResponse({"status": "not_started"})
        return JsonResponse({"status": run.status, "received": run.received_count, "created": run.created_count, "updated": run.updated_count, "error": run.error})
    if request.GET.get("categories") == "1":
        try:
            categories = sync_gifts_categories()
        except CatalogSyncError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        return JsonResponse({"status": "success", "categories": len(categories)})
    full = request.GET.get("full") == "1"
    if full:
        recent_running = CatalogSyncRun.objects.filter(supplier__code="gifts", status="running", started_at__gte=timezone.now() - timedelta(minutes=15)).exists()
        if recent_running:
            return JsonResponse({"status": "already_running"}, status=409)
        manage_path = Path(__file__).resolve().parent.parent / "manage.py"
        subprocess.Popen(
            [sys.executable, str(manage_path), "sync_gifts_catalog"],
            cwd=str(manage_path.parent),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return JsonResponse({"status": "started"}, status=202)
    else:
        try:
            limit = int(request.GET.get("limit", "10"))
        except (TypeError, ValueError):
            return JsonResponse({"error": "Параметр limit должен быть числом от 1 до 100."}, status=400)
        if not 1 <= limit <= 100:
            return JsonResponse({"error": "Параметр limit должен быть от 1 до 100."}, status=400)
    started = time.monotonic()
    try:
        run = sync_gifts_catalog(limit=limit)
    except CatalogSyncError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    products = [
        {"external_id": row["external_id"], "article": row["article"], "name": row["name"]}
        for row in getattr(run, "imported_rows", [])
    ] if not full else []
    return JsonResponse({
        "status": run.status,
        "received": run.received_count,
        "created": run.created_count,
        "updated": run.updated_count,
        "seconds": round(time.monotonic() - started, 2),
        "products": products,
    })


@require_GET
def oasis_import_test(request):
    """Ручной запуск полной синхронизации Oasis, по образцу gifts_import_test
    (?full=1) выше — но ВСЕГДА через отдельный процесс ОС, никогда инлайн:
    у Oasis, в отличие от Gifts, нет бюджетного лимитированного режима,
    полный синк — это единственный режим и ~37 тыс. товаров (~11 мин
    локально). Запускать его в процессе, который обслуживает сайт, нельзя —
    см. docstring tenders/scheduler.py."""
    expected = os.getenv("KNOWLEDGE_SYNC_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
        return HttpResponse(status=403)
    if request.GET.get("status") == "1":
        run = CatalogSyncRun.objects.filter(supplier__code="oasis").first()
        if run is None:
            return JsonResponse({"status": "not_started"})
        return JsonResponse({
            "status": run.status, "received": run.received_count, "created": run.created_count,
            "updated": run.updated_count, "deactivated": run.deactivated_count, "error": run.error,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        })
    recent_running = CatalogSyncRun.objects.filter(
        supplier__code="oasis", status="running", started_at__gte=timezone.now() - timedelta(minutes=30),
    ).exists()
    if recent_running:
        return JsonResponse({"status": "already_running"}, status=409)
    manage_path = Path(__file__).resolve().parent.parent / "manage.py"
    subprocess.Popen(
        [sys.executable, str(manage_path), "sync_oasis_catalog"],
        cwd=str(manage_path.parent),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return JsonResponse({"status": "started"}, status=202)


@require_GET
def gifts_raw_sample(request):
    expected = os.getenv("KNOWLEDGE_SYNC_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
        return HttpResponse(status=403)
    articles = {value.strip() for value in request.GET.get("articles", "1376.89,1376.92").split(",") if value.strip()}
    products = {}
    client = GiftsXmlClient()
    try:
        with client.open("catalogue/product.xml") as product_xml:
            for _, product in ElementTree.iterparse(product_xml, events=("end",)):
                if product.tag.rsplit("}", 1)[-1].lower() != "product":
                    continue
                article = _gifts_text(product, "code")
                if article in articles:
                    products[article] = ElementTree.tostring(product, encoding="unicode")
                product.clear()
                if products.keys() >= articles:
                    break
        stocks = {}
        with client.open("catalogue/stock.xml") as stock_xml:
            for _, stock in ElementTree.iterparse(stock_xml, events=("end",)):
                if stock.tag.rsplit("}", 1)[-1].lower() != "stock":
                    continue
                product_id = _gifts_text(stock, "product_id")
                if product_id in {"16224", "16263"}:
                    stocks[product_id] = ElementTree.tostring(stock, encoding="unicode")
                stock.clear()
        filter_type = ""
        with client.open("catalogue/filters.xml") as filters_xml:
            filters_raw = filters_xml.read().decode("utf-8", errors="replace")
            filter_type = filters_raw[:200000]
    except CatalogSyncError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse({"products": products, "stocks": stocks, "color_filtertype": filter_type, "missing": sorted(articles - products.keys())}, json_dumps_params={"ensure_ascii": False})

def _document_upload_error(upload):
    if upload is None:
        return "Выберите документ."
    if upload.size > 10 * 1024 * 1024:
        return "Файл больше 10 МБ."
    if Path(upload.name).suffix.lower() not in SUPPORTED_TENDER_DOCUMENTS:
        return "Поддерживаются .xlsx, .xls, .doc, .docx и .pdf."
    return ""


def _estimate_for_user(request, pk, model=OrderEstimate):
    estimate = get_object_or_404(model, pk=pk)
    if not request.user.is_superuser and estimate.owner_id != request.user.id:
        raise Http404
    return estimate


@login_required
def legacy_estimate_redirect(request, pk):
    """Keep manager bookmarks from the former /tenders/<id>/ scheme alive.

    Only migrated records retain their former calculation ID, so a legacy
    URL can never accidentally open an unrelated new order calculation.
    """
    estimate = get_object_or_404(OrderEstimate, legacy_calculation_id=pk)
    if not request.user.is_superuser and estimate.owner_id != request.user.id:
        raise Http404
    return redirect("tender_estimate", pk=estimate.pk)


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
        if not sheet.max_row or not sheet.max_column:
            sheet.calculate_dimension(force=True)
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
def document_inspect(request):
    upload = request.FILES.get("file")
    error = _document_upload_error(upload)
    if error:
        return JsonResponse({"error": error}, status=400)
    try:
        return JsonResponse(inspect_tender_document(upload))
    except TenderAIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"error": "Не удалось проверить структуру документа."}, status=400)


@login_required
@require_POST
def ai_import_preview(request):
    upload = request.FILES.get("file")
    error = _document_upload_error(upload)
    if error:
        return JsonResponse({"error": error}, status=400)
    try:
        return JsonResponse(recognize_tender_items(upload))
    except TenderAIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"error": "Не удалось прочитать документ. Проверьте файл и попробуйте ещё раз."}, status=400)


@login_required
@require_POST
def document_preview(request):
    upload = request.FILES.get("file")
    error = _document_upload_error(upload)
    if error:
        return JsonResponse({"error": error}, status=400)
    try:
        raw_lines = json.loads(request.POST.get("lines_json", "[]"))
        current_lines = raw_lines if isinstance(raw_lines, list) else []
    except json.JSONDecodeError:
        current_lines = []
    try:
        requested_role = request.POST.get("document_role", "auto")
        detected_role = detect_tender_document_type(upload)
        if requested_role == "nmck" and detected_role == "technical":
            return JsonResponse({"error": "Этот файл похож на ООЗ или ТЗ. Нажмите «Загрузить ООЗ / ТЗ».", "document_type": detected_role}, status=422)
        if requested_role == "technical" and detected_role == "nmck":
            return JsonResponse({"error": "Этот файл похож на НМЦК. Нажмите «Загрузить НМЦК».", "document_type": detected_role}, status=422)
        role = requested_role if requested_role in {"nmck", "technical"} else detected_role
        result = {"document_type": role, "file_name": upload.name}
        if role in {"nmck", "mixed"}:
            nmck = recognize_tender_items(upload)
            used = set()
            for item in nmck.get("items", []):
                line_index, confidence, reason = _resolve_line_match(None, item.get("name"), item.get("quantity"), current_lines, used)
                item["line_index"] = line_index
                item["match_reason"] = reason
                if line_index is not None:
                    used.add(line_index)
                    item["confidence"] = max(float(item.get("confidence", 0)), confidence)
            result["nmck"] = nmck
        if role in {"technical", "mixed"}:
            result["technical"] = analyze_tender_requirements(upload, current_lines)
        if role == "unknown":
            return JsonResponse({"error": "Не удалось уверенно определить тип документа. Выберите его тип вручную и повторите анализ.", "document_type": role}, status=422)
        return JsonResponse(result)
    except TenderAIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"error": "Не удалось проанализировать документ. Проверьте файл и попробуйте ещё раз."}, status=400)


@login_required
@require_POST
def technical_requirements_preview(request):
    upload = request.FILES.get("file")
    error = _document_upload_error(upload)
    if error:
        return JsonResponse({"error": "Выберите ООЗ или ТЗ." if upload is None else error}, status=400)
    try:
        raw_lines = json.loads(request.POST.get("lines_json", "[]"))
        current_lines = raw_lines if isinstance(raw_lines, list) else []
    except json.JSONDecodeError:
        current_lines = []
    try:
        return JsonResponse(analyze_tender_requirements(upload, current_lines))
    except TenderAIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"error": "Не удалось проанализировать ООЗ/ТЗ. Проверьте файл и попробуйте ещё раз."}, status=400)


@login_required
@require_POST
def production_route_preview(request):
    if not request.user.is_superuser:
        return JsonResponse({"error": "ИИ-расчёт пока доступен только администратору."}, status=403)
    try:
        line = json.loads(request.POST.get("line_json", "{}"))
        if not isinstance(line, dict) or not str(line.get("name", "")).strip() or _number(line.get("quantity")) <= 0:
            raise ValueError
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({"error": "Сначала заполните позицию и примените требования ТЗ."}, status=400)
    start_catalog = request.POST.get("start_catalog") == "1"
    position_name = str(line.get("name", ""))[:500]
    running = ProductionTrainingSession.objects.filter(
        created_by=request.user,
        position_name=position_name,
        is_confirmed=False,
        current_hypothesis__status="processing",
        updated_at__gte=timezone.now() - timedelta(minutes=10),
    ).order_by("-updated_at").first()
    if running is not None:
        return JsonResponse({"status": "processing", "session_id": running.pk}, status=202)
    session = ProductionTrainingSession.objects.create(
        created_by=request.user,
        position_name=position_name,
        requirements=line.get("requirements") if isinstance(line.get("requirements"), dict) else {},
        current_hypothesis={"status": "processing"},
    )

    def work(session):
        hypothesis = build_training_hypothesis(line, progress_callback=lambda stage: _record_stage(session.pk, stage))
        if start_catalog:
            step_id = next(
                (step.get("id") for step in hypothesis.get("route", {}).get("processes", [])
                 if step.get("kind") == "catalog"),
                "",
            )
            hypothesis = build_training_hypothesis(
                line, current=hypothesis, recompute="catalog", step_id=step_id,
                progress_callback=lambda stage: _record_stage(session.pk, stage),
            )
        ProductionTrainingTurn.objects.create(session=session, hypothesis=hypothesis)
        return hypothesis

    _submit_assistant_job(session.pk, work)
    return JsonResponse({"status": "processing", "session_id": session.pk}, status=202)


@login_required
@require_GET
def production_route_status(request, session_id):
    session = get_object_or_404(ProductionTrainingSession, pk=session_id, created_by=request.user)
    hypothesis = session.current_hypothesis if isinstance(session.current_hypothesis, dict) else {}
    if hypothesis.get("status") == "processing":
        return JsonResponse({
            "status": "processing", "session_id": session.pk,
            "stage_label": hypothesis.get("stage_label", ""),
        }, status=202)
    if hypothesis.get("status") == "error":
        return JsonResponse({"error": hypothesis.get("error") or "Не удалось построить расчёт."}, status=400)
    result = dict(hypothesis)
    result["session_id"] = session.pk
    return JsonResponse(result)


@login_required
@require_POST
def revise_production_hypothesis(request):
    if not request.user.is_superuser:
        return JsonResponse({"error": "Обучать ассистента может только администратор."}, status=403)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
        session = ProductionTrainingSession.objects.get(pk=payload.get("session_id"), created_by=request.user, is_confirmed=False)
        line = payload.get("line") if isinstance(payload.get("line"), dict) else {}
        feedback = str(payload.get("feedback", "")).strip()
        question_answers = payload.get("question_answers") if isinstance(payload.get("question_answers"), dict) else None
        # Removing a correction chip resends the reduced instruction list.
        instructions_override = payload.get("instructions") if isinstance(payload.get("instructions"), list) else None
        clear_ranking = bool(payload.get("clear_ranking"))
        # A bare recompute after a server-side change the payload does not
        # itself carry (a lesson was just deactivated).
        refresh = bool(payload.get("refresh"))
        # Which dialogue block's "Учесть и пересчитать" was pressed. The box
        # the admin typed in decides the scope — no LLM guesses which block a
        # comment belongs to. "catalog" keeps the route and search plan
        # untouched; anything else is a full rebuild.
        scope = str(payload.get("scope", "all")).strip().lower()
        learn_for_similar = bool(payload.get("learn_for_similar", not scope.endswith("_current")))
        if scope.endswith("_current"):
            scope = scope.removesuffix("_current")
        step_id = str(payload.get("step_id", "")).strip()
        # "requirements" (the ТЗ-checkbox recompute) is catalog-scoped too —
        # the route and the search plan do not change, only which rows the
        # matcher is allowed to look at.
        recompute = "catalog" if scope in {"catalog", "requirements"} else "production" if scope == "production_price" else "all"
        if len(feedback) > 3000 or not str(line.get("name", "")).strip() or _number(line.get("quantity")) <= 0:
            raise ValueError
        # A "requirements" recompute carries its change in the line payload
        # (the ТЗ-row `selected` flags); a chip removal carries it in
        # instructions_override or clear_ranking; "production_price" needs
        # only step_id — none of these need feedback text.
        if not feedback and question_answers is None and instructions_override is None and not clear_ranking and not refresh and scope not in {"requirements", "catalog", "production_price"}:
            raise ValueError
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError, ProductionTrainingSession.DoesNotExist):
        return JsonResponse({"error": "Не удалось продолжить диалог. Обновите гипотезу и повторите."}, status=400)
    prior = session.current_hypothesis if isinstance(session.current_hypothesis, dict) else {}
    if prior.get("status") == "processing":
        return JsonResponse({"status": "processing", "session_id": session.pk}, status=202)
    if scope == "requirements" and not prior.get("catalog_search_started"):
        prior["requirement_selection"] = line.get("requirements", {}).get("requirements", [])
        session.current_hypothesis = prior
        session.save(update_fields=["current_hypothesis", "updated_at"])
        return JsonResponse({**prior, "session_id": session.pk})
    if question_answers is not None:
        questions = {
            str(question.get("id")): question for question in prior.get("questions", [])
            if isinstance(question, dict) and question.get("id")
        }
        allowed = set(questions)
        answers = {str(key): str(value).strip()[:1000] for key, value in question_answers.items()
                   if str(key) in allowed and str(value).strip()}
        prior = {**prior, "question_answers": {**prior.get("question_answers", {}), **answers}}
        requirements = dict(line.get("requirements")) if isinstance(line.get("requirements"), dict) else {}
        prior_requirements = session.requirements if isinstance(session.requirements, dict) else {}
        existing = requirements.get("clarifications")
        if not isinstance(existing, list):
            existing = prior_requirements.get("clarifications", [])
        clarifications = {
            str(item.get("question_id")): dict(item) for item in existing
            if isinstance(item, dict) and item.get("question_id")
        }
        for question_id, answer in answers.items():
            clarifications[question_id] = {
                "question_id": question_id,
                "label": str(questions[question_id].get("text", "")).strip()[:500],
                "value": answer,
                "source": "Моё уточнение",
                "source_type": "manager_clarification",
                "selected": True,
            }
        requirements["clarifications"] = list(clarifications.values())
        line = {**line, "requirements": requirements}

    def work(session):
        hypothesis = build_training_hypothesis(
            line, current=prior, feedback=feedback,
            progress_callback=lambda stage: _record_stage(session.pk, stage),
            instructions_override=instructions_override, recompute=recompute,
            clear_ranking=clear_ranking,
            learn_for_similar=learn_for_similar,
            **({"step_id": step_id} if step_id else {}),
        )
        session.position_name = str(line.get("name", ""))[:500]
        session.requirements = line.get("requirements") if isinstance(line.get("requirements"), dict) else {}
        ProductionTrainingTurn.objects.create(
            session=session, feedback=feedback,
            understood_changes=hypothesis.get("understood_changes", []), hypothesis=hypothesis,
        )
        return hypothesis

    session.current_hypothesis = {**prior, "status": "processing"}
    session.save(update_fields=["current_hypothesis", "updated_at"])
    _submit_assistant_job(session.pk, work, fallback=prior)
    return JsonResponse({"status": "processing", "session_id": session.pk}, status=202)


@login_required
@require_POST
@transaction.atomic
def confirm_proposed_stage(request):
    """Явно создаёт отдельное предложение из нижней панели.

    Оно никогда не подменяет шаг текущего маршрута: после создания маршрут
    нужно построить заново, и только тогда новый этап может быть выбран.
    """
    if not request.user.is_superuser:
        return JsonResponse({"error": "Создавать этапы может только администратор."}, status=403)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
        session = ProductionTrainingSession.objects.select_for_update().get(
            pk=payload.get("session_id"), created_by=request.user, is_confirmed=False,
        )
        proposal_id = str(payload.get("proposal_id", "")).strip()
        hypothesis = session.current_hypothesis if isinstance(session.current_hypothesis, dict) else {}
        proposals = hypothesis.get("stage_proposals", [])
        proposed = next((item for item in proposals if isinstance(item, dict) and item.get("id") == proposal_id), None)
        if not isinstance(proposed, dict):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError, ProductionTrainingSession.DoesNotExist):
        return JsonResponse({"error": "Этап не найден или маршрут устарел. Обновите расчёт."}, status=400)

    role = proposed.get("role")
    stage_payload = {
        "name": proposed.get("name"),
        "description": proposed.get("description", ""),
        "supplies_input": role == "supply",
        "performs_production": role == "production",
        "terminal_mode": "always" if role == "completion" else "sometimes",
    }
    proposal = Proposal.objects.create(
        type=Proposal.TYPE_CREATE_STAGE, payload=stage_payload,
        summary=f"Новый этап «{stage_payload['name']}» — предложен из маршрута", created_by=request.user,
    )
    apply_proposal(proposal, request.user)
    stage = ProcessDefinition.objects.get(name=stage_payload["name"])

    hypothesis["stage_proposals"] = [item for item in proposals if item.get("id") != proposal_id]
    session.current_hypothesis = hypothesis
    session.save(update_fields=["current_hypothesis", "updated_at"])
    return JsonResponse({**hypothesis, "session_id": session.pk, "route_refresh_required": True})


@login_required
@require_POST
def drop_requirement_skip_rule(request):
    """Undo a learned "не участвует в подборе" — the row of this label goes
    back to being checked by default in future tenders."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "Менять правила может только администратор."}, status=403)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
        label_normalized = _normalized_requirement_label(str(payload.get("label", "")))[:200]
        if not label_normalized:
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Не удалось определить строку ТЗ."}, status=400)
    removed = RequirementSkipRule.objects.filter(label_normalized=label_normalized).update(is_active=False)
    return JsonResponse({"dropped": bool(removed)})


@login_required
@require_GET
def production_base_data(request):
    """Список этапов и контрагентов для экрана «База производства» внутри
    выдвижной панели ассистента — только чтение; создание и правка этапов/
    контрагентов идут отдельным шагом."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "База производства доступна только администратору."}, status=403)
    stages = [{
        "id": stage.pk, "name": stage.name, "supplies_input": stage.supplies_input,
        "performs_production": stage.performs_production, "terminal_mode": stage.terminal_mode,
        "scope_tags": stage.scope_tags, "is_active": stage.is_active,
        "counterparty_count": stage.counterparty_links.count(),
    } for stage in ProcessDefinition.objects.order_by("name")]
    counterparties = []
    for counterparty in Counterparty.objects.prefetch_related("stage_links__stage").order_by("name"):
        links = list(counterparty.stage_links.all())
        counterparties.append({
            "id": counterparty.pk, "name": counterparty.name, "is_active": counterparty.is_active,
            "stage_names": [link.stage.name for link in links],
            "price_sources": sorted({link.get_price_source_type_display() for link in links}),
        })
    return JsonResponse({"stages": stages, "counterparties": counterparties})


@login_required
@require_POST
def production_counterparty_draft(request):
    """Первый шаг добавления контрагента (§13 промпта): текст, ссылка или
    скриншот → черновик название+сводка+предложенные этапы. Сырой источник
    сохраняется сразу (это только улика, не решение — как и у существующих
    источников расчёта), а сам Counterparty — только на подтверждении."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "Добавлять контрагентов может только администратор."}, status=403)
    text = request.POST.get("text", "")
    url = request.POST.get("url", "")
    upload = request.FILES.get("file")
    if upload is not None:
        if Path(upload.name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            return JsonResponse({"error": "Пока принимаем скриншот (PNG/JPG), текст или ссылку — без PDF/Excel."}, status=400)
        if upload.size > 20 * 1024 * 1024:
            return JsonResponse({"error": "Файл больше 20 МБ."}, status=400)
    try:
        source = extract_calculation_source(source_text=text, source_url=url, upload=upload)
        active_stages = list(ProcessDefinition.objects.filter(is_active=True).values("id", "name"))
        draft = parse_counterparty_draft(source["content"], [stage["name"] for stage in active_stages])
    except TenderAIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    raw_file, raw_file_name, raw_file_content_type = None, "", ""
    if upload is not None:
        upload.seek(0)
        raw_file, raw_file_name, raw_file_content_type = upload.read(), upload.name[:255], (upload.content_type or "")[:100]
    knowledge_source = TenderKnowledgeSource.objects.create(
        title=draft["name"][:300], source_type=source["source_type"], url=source["url"],
        content_summary=source["content"][:4000], structured_data=source["structured_data"],
        raw_file=raw_file, raw_file_name=raw_file_name, raw_file_content_type=raw_file_content_type,
        created_by=request.user,
    )
    return JsonResponse({
        "source_id": knowledge_source.pk, "name": draft["name"], "notes": draft["notes"],
        "suggested_stage_names": draft["suggested_stage_names"], "active_stages": active_stages,
    })


@login_required
@require_POST
@transaction.atomic
def production_counterparty_confirm(request):
    """Второй шаг: администратор проверил/поправил черновик и подтвердил —
    только теперь создаётся Counterparty и связи с выбранными этапами, оба
    через Proposal (см. tenders/proposals.py), одним batch на карточку."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "Добавлять контрагентов может только администратор."}, status=403)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
        name = str(payload.get("name", "")).strip()[:200]
        if not name:
            raise ValueError
        notes = str(payload.get("notes", ""))[:2000]
        stage_ids = [int(value) for value in payload.get("stage_ids", []) if str(value).isdigit()]
        source_id = payload.get("source_id")
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Не удалось прочитать форму контрагента."}, status=400)

    counterparty_proposal = Proposal.objects.create(
        type=Proposal.TYPE_CREATE_COUNTERPARTY, payload={"name": name, "notes": notes},
        summary=f"Новый контрагент «{name}»", created_by=request.user,
    )
    apply_proposal(counterparty_proposal, request.user)
    counterparty = Counterparty.objects.get(name=name)
    stages = {stage.pk: stage for stage in ProcessDefinition.objects.filter(pk__in=stage_ids)}
    for stage_id in stage_ids:
        stage = stages.get(stage_id)
        if stage is None:
            continue
        link_proposal = Proposal.objects.create(
            type=Proposal.TYPE_LINK_STAGE_COUNTERPARTY, batch_id=counterparty_proposal.batch_id,
            payload={"stage_id": stage.pk, "counterparty_id": counterparty.pk},
            summary=f"Связать «{stage.name}» и «{name}»", created_by=request.user,
        )
        apply_proposal(link_proposal, request.user)
    if source_id:
        TenderKnowledgeSource.objects.filter(pk=source_id, counterparty__isnull=True).update(counterparty=counterparty)
    linked_stage_names = list(counterparty.stage_links.select_related("stage").values_list("stage__name", flat=True))
    return JsonResponse({"id": counterparty.pk, "name": counterparty.name, "stage_names": linked_stage_names})


@login_required
@require_POST
def production_feedback_draft(request):
    """Свободный текст администратора про «Базу производства» → черновик
    из одного или нескольких Proposal (сразу сохранены как pending — это
    и есть журнал «что предложено», см. tenders/proposals.py). Ничего не
    применяется здесь — только на production_feedback_confirm."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "Учить «Базу производства» может только администратор."}, status=403)
    text = request.POST.get("text", "").strip()
    if not text:
        return JsonResponse({"error": "Напишите, что изменить."}, status=400)
    active_stages = list(ProcessDefinition.objects.filter(is_active=True).values("id", "name"))
    active_counterparties = list(Counterparty.objects.filter(is_active=True).values("id", "name"))
    try:
        items = parse_production_feedback(text, active_stages, active_counterparties)
    except TenderAIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    batch_id = None
    proposals = []
    for item in items:
        proposal = Proposal.objects.create(
            type=item["type"], payload=payload_from_feedback_item(item["type"], item["raw"]),
            summary=item["summary"], source_text=text, created_by=request.user,
            **({"batch_id": batch_id} if batch_id else {}),
        )
        batch_id = batch_id or proposal.batch_id
        proposals.append(proposal)
    return JsonResponse({
        "batch_id": str(batch_id),
        "items": [{"id": p.pk, "type": p.type, "type_display": p.get_type_display(), "summary": p.summary, "payload": p.payload} for p in proposals],
        "active_stages": active_stages, "active_counterparties": active_counterparties,
    })


@login_required
@require_POST
def production_feedback_confirm(request):
    """Администратор отметил, какие пункты черновика принять — остальные
    из этого же batch_id отклоняются (см. proposals.apply_batch)."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "Учить «Базу производства» может только администратор."}, status=403)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
        batch_id = payload["batch_id"]
        accepted_ids = [int(value) for value in payload.get("accepted_ids", [])]
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Не удалось прочитать список правок."}, status=400)
    try:
        decided = apply_batch(batch_id, accepted_ids, request.user)
    except TenderAIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"results": [{"id": p.pk, "status": p.status, "summary": p.summary} for p in decided]})


@login_required
@require_POST
def drop_lesson(request):
    """Deactivate a learned lesson (the "Учтён прошлый опыт" chip) — it
    stops being fed into the shortlist pass on every position. Reversible
    from the admin (is_active), never hard-deleted here."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "Менять уроки может только администратор."}, status=403)
    try:
        lesson_id = int(json.loads(request.POST.get("payload", "{}")).get("lesson_id"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Не удалось определить урок."}, status=400)
    dropped = Lesson.objects.filter(pk=lesson_id, is_active=True).update(is_active=False)
    return JsonResponse({"dropped": bool(dropped)})


@login_required
@require_POST
@transaction.atomic
def drop_route_knowledge(request):
    if not request.user.is_superuser:
        return JsonResponse({"error": "Менять знания может только администратор."}, status=403)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
        if payload.get("example_id"):
            example_id = int(payload["example_id"])
            changed = ProductionTrainingExample.objects.filter(pk=example_id).update(is_active=False)
            Lesson.objects.filter(session__confirmed_example_id=example_id, scope__in=["route", "production_step"]).update(is_active=False)
        else:
            lesson = Lesson.objects.get(pk=int(payload["lesson_id"]), scope__in=["route", "production_step"])
            lesson.is_active = False
            lesson.save(update_fields=["is_active"])
            # The confirmed route also contains this correction; stop recalling both representations.
            if lesson.session_id and lesson.session.confirmed_example_id:
                ProductionTrainingExample.objects.filter(pk=lesson.session.confirmed_example_id).update(is_active=False)
            changed = True
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, Lesson.DoesNotExist):
        return JsonResponse({"error": "Не удалось определить сохранённый опыт."}, status=400)
    return JsonResponse({"dropped": bool(changed)})


@login_required
@require_POST
def select_catalog_product(request):
    if not request.user.is_superuser:
        return JsonResponse({"error": "Выбирать товары для обучения может только администратор."}, status=403)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
        session = ProductionTrainingSession.objects.get(pk=payload.get("session_id"), created_by=request.user, is_confirmed=False)
        line = payload.get("line") if isinstance(payload.get("line"), dict) else {}
        product_id = str(payload.get("product_id", "")).strip()[:100]
        if not product_id:
            raise ValueError
        if not str(line.get("name", "")).strip():
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError, ProductionTrainingSession.DoesNotExist):
        return JsonResponse({"error": "Не удалось выбрать товар поставщика. Обновите гипотезу."}, status=400)
    try:
        prior = session.current_hypothesis
        if prior.get("route", {}).get("schema_version") == 1:
            from .routes import catalog_step_state, merge_catalog_step
            step, state = catalog_step_state(prior, str(payload.get("step_id", "")))
            selected = apply_catalog_candidate({**prior, **state}, line, product_id)
            hypothesis = merge_catalog_step(prior, selected, line, step["id"])
        else:
            hypothesis = apply_catalog_candidate(prior, line, product_id)
        hypothesis["session_id"] = session.pk
        session.current_hypothesis = hypothesis
        session.save(update_fields=["current_hypothesis", "updated_at"])
        selection = hypothesis.get("catalog_selection", {})
        CatalogMatchDecision.objects.create(
            session=session,
            product=None,
            supplier_code=str(selection.get("supplier_code") or "oasis")[:50],
            product_external_id=str(selection.get("external_id") or selection.get("id") or "")[:100],
            product_article=str(selection.get("article") or "")[:120],
            product_snapshot={
                key: selection.get(key) for key in (
                    "external_id", "article", "name", "price", "stock", "url", "category", "matches",
                    "supplier_code", "supplier_name", "supplier_site",
                )
            },
            decision="selected",
            reason_codes=[
                "backend_exact_match" if selection.get("fit") == "exact" else "admin_override_mismatch",
                "selected_by_admin",
            ],
            requirement_signature={
                "name": str(line.get("name", ""))[:500],
                "quantity": str(line.get("quantity", ""))[:50],
                "requirements": line.get("requirements") if isinstance(line.get("requirements"), dict) else {},
            },
            created_by=request.user,
        )
        supplier_name = str(selection.get("supplier_name") or selection.get("supplier_code") or "поставщика")
        change = f"Выбран товар поставщика {supplier_name}: {selection.get('name', 'товар')} · арт. {selection.get('article', '')}".strip()
        ProductionTrainingTurn.objects.create(session=session, feedback=change, understood_changes=[change], hypothesis=hypothesis)
        return JsonResponse(hypothesis)
    except TenderAIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"error": "Не удалось применить товар поставщика к расчёту."}, status=400)


@login_required
@require_POST
def add_calculation_source(request):
    if not request.user.is_superuser:
        return JsonResponse({"error": "Добавлять источники расчёта может только администратор."}, status=403)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
        session = ProductionTrainingSession.objects.get(pk=payload.get("session_id"), created_by=request.user, is_confirmed=False)
        line = payload.get("line") if isinstance(payload.get("line"), dict) else {}
        hypothesis = session.current_hypothesis if isinstance(session.current_hypothesis, dict) else {}
        costs = hypothesis.get("costs") if isinstance(hypothesis.get("costs"), list) else []
        if not str(line.get("name", "")).strip():
            raise ValueError
        raw_sources = payload.get("sources") if isinstance(payload.get("sources"), list) else [payload]
        if not raw_sources or len(raw_sources) > 6:
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError, ProductionTrainingSession.DoesNotExist, TenderKnowledgeSource.DoesNotExist):
        return JsonResponse({"error": "Не удалось определить статью расчёта или источник."}, status=400)

    try:
        prepared_sources = []
        batch_mode = isinstance(payload.get("sources"), list)
        administrator_feedback = str(payload.get("feedback", "")).strip()[:6000]
        for source_index, raw_source in enumerate(raw_sources):
            if not isinstance(raw_source, dict):
                raise ValueError
            raw_cost_index = raw_source.get("cost_index")
            cost_index = int(raw_cost_index) if raw_cost_index not in (None, "") else None
            if cost_index is not None and (cost_index < 0 or cost_index >= len(costs)):
                raise ValueError
            upload = request.FILES.get(f"file_{source_index}" if batch_mode else "file")
            if upload is not None and upload.size > 10 * 1024 * 1024:
                return JsonResponse({"error": f"Файл источника № {source_index + 1} больше 10 МБ."}, status=400)
            existing = None
            target_name = costs[cost_index].get("name", "") if cost_index is not None else ""
            if raw_source.get("source_id"):
                existing = TenderKnowledgeSource.objects.get(pk=raw_source["source_id"], is_active=True)
            if existing:
                if existing.url:
                    extracted = extract_calculation_source(
                        source_url=existing.url,
                        selection_context={"line": line, "feedback": administrator_feedback, "target_name": target_name},
                    )
                    refreshed = extracted.get("structured_data") if isinstance(extracted.get("structured_data"), dict) else {}
                    existing.content_summary = extracted["content"]
                    existing.structured_data = {
                        **(existing.structured_data if isinstance(existing.structured_data, dict) else {}),
                        **refreshed,
                    }
                    existing.save(update_fields=["content_summary", "structured_data", "updated_at"])
                else:
                    extracted = {
                        "content": existing.content_summary,
                        "source_type": existing.source_type,
                        "url": existing.url,
                        "structured_data": existing.structured_data if isinstance(existing.structured_data, dict) else {},
                    }
                source = existing
            else:
                extracted = extract_calculation_source(
                    source_text=str(raw_source.get("source_text", "")),
                    source_url=str(raw_source.get("source_url", "")),
                    upload=upload,
                    selection_context={"line": line, "feedback": administrator_feedback, "target_name": target_name},
                )
                title = str(raw_source.get("title", "")).strip() or str(raw_source.get("supplier_name", "")).strip() or target_name or f"Источник для {line.get('name')}"
                extracted_structured = extracted.get("structured_data") if isinstance(extracted.get("structured_data"), dict) else {}
                source = TenderKnowledgeSource.objects.create(
                    title=title[:300],
                    supplier_name=str(raw_source.get("supplier_name", "")).strip()[:200],
                    source_type=extracted["source_type"],
                    url=extracted["url"],
                    content_summary=extracted["content"],
                    structured_data={
                        "scope": "cost" if cost_index is not None else "position",
                        "position_name": str(line.get("name", ""))[:300],
                        "cost_name": target_name,
                        **extracted_structured,
                    },
                    created_by=request.user,
                    is_active=False,
                )
            prepared_sources.append({
                "source": source,
                "extracted": extracted,
                "cost_index": cost_index,
                "target": costs[cost_index] if cost_index is not None else None,
            })

        feedback_parts = [administrator_feedback] if administrator_feedback else []
        for source_index, prepared in enumerate(prepared_sources, start=1):
            source, target, extracted = prepared["source"], prepared["target"], prepared["extracted"]
            if target is not None:
                instruction = (
                    f"Для статьи «{target.get('name', 'расход')}» добавлен проверяемый источник «{source}». "
                    "Пересчитай эту статью по данным источника, не копируй итог из похожего заказа."
                )
            else:
                instruction = (
                    f"К позиции добавлен проверяемый источник «{source}». Сам определи, к какому процессу, "
                    "материалу или статье цены он относится. Используй его только в подходящем маршруте; "
                    "наличие источника само по себе не подтверждает маршрут и не делает его оптимальным."
                )
            feedback_parts.append(f"ИСТОЧНИК № {source_index}. {instruction}\nДАННЫЕ ИСТОЧНИКА:\n{extracted['content'][:12000]}")
        feedback_parts.append("Сохрани универсальные процессы, подробные формулы, все промежуточные действия и способы адаптации к текущему тиражу. Не смешивай предложения разных поставщиков в одну цену.")
        feedback = "\n\n".join(feedback_parts)
        updated = build_training_hypothesis(line, current=hypothesis, feedback=feedback)
        for prepared in prepared_sources:
            structured = prepared["extracted"].get("structured_data") if isinstance(prepared["extracted"].get("structured_data"), dict) else {}
            updated = apply_verified_source_quote(
                updated,
                line,
                structured.get("price_quote"),
                str(prepared["source"]),
                prepared["source"].url,
                prepared["target"],
            )
        updated["session_id"] = session.pk
        attached_sources = hypothesis.get("sources") if isinstance(hypothesis.get("sources"), list) else []
        new_source_ids = {prepared["source"].pk for prepared in prepared_sources}
        source_cards = [{
            "id": prepared["source"].pk,
            "title": prepared["source"].title,
            "supplier_name": prepared["source"].supplier_name,
            "source_type": prepared["source"].source_type,
            "url": prepared["source"].url,
            "scope": "cost" if prepared["target"] is not None else "position",
            "cost_name": prepared["target"].get("name", "") if prepared["target"] is not None else "",
            "is_pending": not prepared["source"].is_active,
        } for prepared in prepared_sources]
        updated["sources"] = [value for value in attached_sources if value.get("id") not in new_source_ids] + source_cards
        updated_costs = updated.get("costs") if isinstance(updated.get("costs"), list) else []
        for prepared in prepared_sources:
            source, target, cost_index = prepared["source"], prepared["target"], prepared["cost_index"]
            if target is None:
                continue
            matching = next((item for item in updated_costs if str(item.get("name", "")).casefold() == str(target.get("name", "")).casefold()), None)
            if matching is None and cost_index < len(updated_costs):
                matching = updated_costs[cost_index]
            if matching is not None:
                structured = prepared["extracted"].get("structured_data") if isinstance(prepared["extracted"].get("structured_data"), dict) else {}
                matching.update({
                    "source": str(source), "source_id": source.pk,
                    "source_type": "supplier" if structured.get("price_quote") else source.source_type,
                    "source_url": source.url, "source_date": source.updated_at.date().isoformat(),
                })
        session.current_hypothesis = updated
        session.save(update_fields=["current_hypothesis", "updated_at"])
        ProductionTrainingTurn.objects.create(
            session=session, feedback=feedback, understood_changes=updated.get("understood_changes", []), hypothesis=updated,
        )
        return JsonResponse(updated)
    except TenderAIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"error": "Не удалось прочитать источник или пересчитать статью."}, status=400)


@login_required
@require_POST
@transaction.atomic
def confirm_production_type(request):
    if not request.user.is_superuser:
        return JsonResponse({"error": "Добавлять учебные примеры может только администратор."}, status=403)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
        line = payload.get("line") if isinstance(payload.get("line"), dict) else {}
        session_id = payload.get("session_id")
        if session_id:
            # "Принять и обучить" accepts the calculation and writes every
            # catalog/requirements instruction the admin gave this session
            # to the Lesson table, with the context it was learned in. A
            # run with nothing to learn (a product picked, no feedback) is
            # fine — it just closes the session.
            session = ProductionTrainingSession.objects.select_for_update().get(pk=session_id, created_by=request.user, is_confirmed=False)
            hypothesis = session.current_hypothesis if isinstance(session.current_hypothesis, dict) else {}
            if hypothesis.get("status") in {"processing", "error"}:
                return JsonResponse({"error": "Дождитесь успешного построения маршрута."}, status=400)
            saved = 0
            if hypothesis.get("catalog_steps"):
                for step_id, state in hypothesis["catalog_steps"].items():
                    instructions = [entry for entry in hypothesis.get("session_instructions", []) if entry.get("step_id", step_id) == step_id]
                    saved += learn_lessons_from_session({**state, "session_instructions": instructions}, session, request.user)
            else:
                saved = learn_lessons_from_session(hypothesis, session, request.user)
            from .routes import confirm_route
            saved += confirm_route(hypothesis, session, request.user)
            # Learn every ТЗ row the admin left unchecked — its label comes
            # pre-unchecked in every future tender (RequirementSkipRule).
            skipped = 0
            selection = hypothesis.get("requirement_selection", [])
            for row in selection if isinstance(selection, list) else []:
                if (not isinstance(row, dict) or row.get("selected") is not False
                        or row.get("source_type") == "manager_clarification"):
                    continue
                label = str(row.get("label", "")).strip()[:200]
                label_normalized = _normalized_requirement_label(label)
                if not label_normalized:
                    continue
                _, created = RequirementSkipRule.objects.get_or_create(
                    label_normalized=label_normalized,
                    defaults={"label": label, "example_value": str(row.get("value", ""))[:500], "created_by": request.user, "is_active": True},
                )
                if not created:
                    RequirementSkipRule.objects.filter(label_normalized=label_normalized, is_active=False).update(is_active=True)
                skipped += int(created)
            session.is_confirmed = True
            session.current_hypothesis = {**hypothesis, "is_confirmed": True}
            session.save(update_fields=["is_confirmed", "current_hypothesis", "updated_at"])
            parts = ["Маршрут принят и сохранён." if session.confirmed_example_id else "Расчёт принят."]
            parts.append(f"Новых уроков: {saved}." if saved else "Новых уроков нет.")
            if skipped:
                parts.append(f"Строк ТЗ вынесено из подбора навсегда: {skipped}.")
            return JsonResponse({"message": " ".join(parts), "lessons_saved": saved, "rules_saved": saved, "requirement_skips_saved": skipped})
        production_type = ProductionType.objects.get(code=payload.get("production_type"), is_active=True)
        name = str(line.get("name", "")).strip()
        if not name:
            raise ValueError
        features = payload.get("features") if isinstance(payload.get("features"), list) else []
        raw_routes = payload.get("routes") if isinstance(payload.get("routes"), list) else []
    except (ValueError, TypeError, json.JSONDecodeError, ProductionType.DoesNotExist, ProductionTrainingSession.DoesNotExist):
        return JsonResponse({"error": "Не удалось сохранить учебный пример."}, status=400)
    routes = []
    for route_index, raw_route in enumerate(raw_routes[:5]):
        if not isinstance(raw_route, dict):
            continue
        processes = []
        for raw_process in raw_route.get("processes", [])[:12]:
            if not isinstance(raw_process, dict):
                continue
            role = raw_process.get("role") if raw_process.get("role") in {"supply", "production", "completion"} else "production"
            process_name = str(raw_process.get("name", "")).strip()[:200]
            if not process_name:
                continue
            ProcessDefinition.objects.get_or_create(name=process_name, role=role)
            processes.append({"role": role, "name": process_name, "reason": str(raw_process.get("reason", ""))[:300]})
        if processes:
            routes.append({"name": str(raw_route.get("name", "")).strip()[:120] or f"Маршрут {route_index + 1}", "reason": str(raw_route.get("reason", ""))[:300], "processes": processes})
    if not routes:
        return JsonResponse({"error": "Добавьте хотя бы один процесс в маршрут."}, status=400)
    example = ProductionTrainingExample.objects.create(
        production_type=production_type,
        position_name=name[:500],
        requirements=line.get("requirements") if isinstance(line.get("requirements"), dict) else {},
        features=[str(value)[:300] for value in features[:10]],
        routes=routes,
        note=str(payload.get("note", ""))[:500],
        created_by=request.user,
    )
    refresh_training_example_embedding(example)
    ProductionTrainingExample.objects.filter(
        production_type=production_type,
        position_name__iexact=name,
        is_active=True,
    ).exclude(pk=example.pk).update(is_active=False, superseded_by=example)
    return JsonResponse({"message": f"Пример сохранён: {production_type.name}.", "example_id": example.pk})


@login_required
@require_POST
def calculator_knowledge_proposal(request):
    return JsonResponse({"error": "Добавьте постоянный расходник через калькулятор или админку."}, status=410)


def _persist_tender_estimate(request, estimate, settings, *, pipeline=False):
    """Parse the posted tender state and write it to ``estimate`` (creating
    one when it is ``None``). Returns ``(estimate, error, meta)`` — ``error``
    is a message string when nothing was saved, ``meta`` carries
    ``incomplete`` and the parsed ``posted_lines`` / ``posted_analysis``
    (kept even on a validation error so the form can re-render them).
    Shared by the plain form POST and the autosave endpoint."""
    meta = {"incomplete": False, "posted_lines": None, "posted_analysis": None}
    try:
        raw_lines = json.loads(request.POST.get("lines_json", "[]"))
        raw_analysis = json.loads(request.POST.get("document_analysis_json", "{}"))
    except json.JSONDecodeError:
        return estimate, "Проверьте товарные позиции.", meta
    meta["posted_lines"] = raw_lines if isinstance(raw_lines, list) else []
    meta["posted_analysis"] = raw_analysis if isinstance(raw_analysis, dict) else {}
    try:
        tender_number = request.POST.get("tender_number", "").strip()
        name = request.POST.get("name", "").strip()
        reduction_percent = _number(request.POST.get("reduction_percent"), str(settings.default_reduction_percent))
        russia_delivery = _number(request.POST.get("russia_delivery"))
        if not tender_number or not name or not Decimal("0") <= reduction_percent <= Decimal("100") or russia_delivery < 0:
            raise ValueError
        lines = []
        calculation_complete = True
        for row in meta["posted_lines"]:
            line = {
                "name": str(row.get("name", "")).strip(),
                "quantity": _number(row.get("quantity")),
                "nmck_unit": _number(row.get("nmck_unit")),
                "material_unit": _number(row.get("material_unit")),
                "application_unit": _number(row.get("application_unit")),
                "logistics_unit": _number(row.get("logistics_unit")),
                "product_url": str(row.get("product_url", "")).strip(),
                "comment": str(row.get("comment", "")).strip(),
                "requirements": row.get("requirements") if isinstance(row.get("requirements"), dict) else {},
            }
            has_expense = any(line[key] > 0 for key in ("material_unit", "application_unit", "logistics_unit"))
            if min(line["quantity"], line["nmck_unit"], line["material_unit"], line["application_unit"], line["logistics_unit"]) < 0:
                raise ValueError
            if not line["name"] or line["quantity"] <= 0 or line["nmck_unit"] <= 0 or not has_expense:
                calculation_complete = False
            lines.append(line)
        if not lines:
            lines.append({"name": "", "quantity": Decimal("0"), "nmck_unit": Decimal("0"), "material_unit": Decimal("0"), "application_unit": Decimal("0"), "logistics_unit": Decimal("0"), "product_url": "", "comment": "", "requirements": {}})
            calculation_complete = False
    except (ValueError, TypeError, InvalidOperation):
        return estimate, "Проверьте реквизиты тендера и товарные позиции.", meta

    calculated, summary = calculate_tender(lines, reduction_percent, russia_delivery, settings.vat_rate)
    is_new = estimate is None
    estimate = estimate or (TenderEstimate(owner=request.user) if pipeline else OrderEstimate(owner=request.user))
    if request.user.is_superuser and request.POST.get("owner_id"):
        estimate.owner = get_object_or_404(get_user_model(), pk=request.POST["owner_id"])
    estimate.tender_number = tender_number[:100]
    estimate.name = name[:300]
    estimate.reduction_percent = reduction_percent
    estimate.russia_delivery = russia_delivery
    estimate.result_notes = request.POST.get("result_notes", "").strip()[:5000]
    estimate.vat_rate_snapshot = settings.vat_rate
    estimate.summary_snapshot = {key: str(value) for key, value in summary.items()}
    estimate.summary_snapshot["is_incomplete"] = not calculation_complete
    estimate.document_analysis = meta["posted_analysis"] or {}
    if not pipeline and not estimate.order_id:
        estimate.order = Order.objects.create(name=estimate.name)
    elif not pipeline:
        estimate.order.name = estimate.name
        estimate.order.save(update_fields=["name", "updated_at"])
    estimate.save()
    estimate.lines.all().delete()
    line_model = TenderLine if pipeline else OrderLine
    line_model.objects.bulk_create([line_model(estimate=estimate, sort_order=index, **line) for index, line in enumerate(lines)])
    meta["incomplete"] = not calculation_complete
    return estimate, None, meta


def _saved_estimates_for(request):
    """Список «Расчёты»: свои OrderEstimate и TenderEstimate в одной ленте.

    Каждой строке нужен свой набор маршрутов (order/* против pipeline/*),
    поэтому они дописываются на объект — шаблон видит один однородный список.
    """
    from tender_selection.models import Tender

    order_qs = OrderEstimate.objects.all()
    # Тот же критерий, что и на канбане: тендер виден, пока не архивирован
    # («Расчёт» → «Торги» → «Результат»); архивный — пропадает отовсюду разом.
    tender_qs = TenderEstimate.objects.filter(tender__isnull=False).exclude(tender__status=Tender.DISMISSED)
    if not request.user.is_superuser:
        order_qs = order_qs.filter(owner=request.user)
        tender_qs = tender_qs.filter(owner=request.user)

    # Пусто в query string ("kind=") — пользователь явно выбрал «Все»;
    # параметра вовсе нет — свежий заход на страницу, показываем тендеры по умолчанию.
    kind = request.GET.get("kind") if "kind" in request.GET else "tender"
    if kind == "order":
        tender_qs = TenderEstimate.objects.none()
    elif kind == "tender":
        order_qs = OrderEstimate.objects.none()

    combined = list(order_qs) + list(tender_qs)
    for estimate in combined:
        is_tender = isinstance(estimate, TenderEstimate)
        estimate.is_tender_estimate = is_tender
        if is_tender:
            # Тендерный расчёт нельзя переименовать/скопировать/удалить со
            # страницы расчёта — только row_url, без duplicate/delete.
            estimate.row_url = reverse("tender_worklist_estimate", args=[estimate.pk])
        else:
            estimate.row_url = reverse("tender_estimate", args=[estimate.pk])
            estimate.duplicate_url = reverse("tender_estimate_duplicate", args=[estimate.pk])
            estimate.delete_url = reverse("tender_estimate_delete", args=[estimate.pk])

    worklist = request.GET.get("worklist") if "worklist" in request.GET else "active"
    if worklist == "active":
        combined = [e for e in combined if e.is_active_task()]
    elif worklist == "ready":
        combined = [e for e in combined if not e.is_active_task()]

    combined.sort(key=lambda e: e.updated_at, reverse=True)
    return combined[:12], kind, worklist


def _risk_summary_for(tender):
    """Сжатая выжимка для менеджера, у которого нет доступа к самой карточке
    тендера: срок подачи заявки — всегда (это базовый факт тендера, не часть
    оценки), остальное — из оценки риска, если она уже была. ``None`` только
    если тендер не найден (расчёт остался без своей записи)."""
    if not tender:
        return None
    from tender_selection.views import _risk_badge

    risk = tender.risk_assessment or {}
    delivery_mode = risk.get("delivery_mode")
    batch_days = (risk.get("risk_facts") or {}).get("batch_days")
    # Срок на одну заявку — деталь именно способа поставки, не срока
    # исполнения контракта целиком, поэтому идёт тут, а не в "execution".
    if delivery_mode and batch_days:
        delivery_mode = f"{delivery_mode} (по одной заявке — {batch_days} дн.)"
    return {
        "risk_badge": _risk_badge(tender),
        "submission_deadline": tender.collecting_finished_at,
        "execution": (risk.get("execution_deadline") or {}).get("date"),
        "delivery_mode": delivery_mode,
        "sample_requirements": risk.get("sample_requirements"),
        "national_regime": risk.get("national_regime"),
        "risk_factors": risk.get("risk_factors"),
        "risk_level": risk.get("risk_level"),
    }


@login_required
def home(request, pk=None, pipeline=False, minimal=False):
    """Одно и то же ядро (таблица + калькулятор) в трёх средах:
    ``order`` — самостоятельный расчёт, ``pipeline`` — минимальный вид из
    карточки тендера (только счёт и «применить и вернуться»), ``worklist`` —
    тот же TenderEstimate, но открытый из списка «Расчёты» (с блоком рисков,
    без прямой ссылки на карточку тендера, которая менеджеру недоступна)."""
    shell = "order" if not pipeline else ("pipeline" if minimal else "worklist")
    route_prefix = {"order": "tender_estimate", "pipeline": "tender_pipeline_estimate", "worklist": "tender_worklist_estimate"}[shell]
    model = TenderEstimate if pipeline else OrderEstimate
    estimate = _estimate_for_user(request, pk, model) if pk else None
    settings = TenderSettings.objects.get_or_create(pk=1)[0]
    posted_lines = None
    posted_analysis = None
    form_state = {
        "tender_number": estimate.tender_number if estimate else "",
        "name": estimate.name if estimate else "",
        "reduction_percent": estimate.reduction_percent if estimate else settings.default_reduction_percent,
        "russia_delivery": estimate.russia_delivery if estimate else Decimal("0.00"),
        "result_notes": estimate.result_notes if estimate else "",
        "owner_id": estimate.owner_id if estimate else request.user.id,
    }
    if request.method == "POST":
        form_state = {
            "tender_number": request.POST.get("tender_number", ""),
            "name": request.POST.get("name", ""),
            "reduction_percent": request.POST.get("reduction_percent", str(settings.default_reduction_percent)),
            "russia_delivery": request.POST.get("russia_delivery", "0"),
            "result_notes": request.POST.get("result_notes", ""),
            "owner_id": request.POST.get("owner_id") or request.user.id,
        }
        estimate, error, meta = _persist_tender_estimate(request, estimate, settings, pipeline=pipeline)
        posted_lines = meta["posted_lines"]
        posted_analysis = meta["posted_analysis"]
        if error:
            messages.error(request, error)
        else:
            messages.success(request, "Черновик просчёта сохранён." if meta["incomplete"] else "Просчёт тендера сохранён.")
            return redirect(route_prefix, pk=estimate.pk)

    initial_lines = []
    if posted_lines is not None:
        initial_lines = posted_lines
    elif estimate:
        initial_lines = [{"id": line.pk, "name": line.name, "quantity": str(line.quantity), "nmck_unit": str(line.nmck_unit), "material_unit": str(line.material_unit), "application_unit": str(line.application_unit), "logistics_unit": str(line.logistics_unit), "product_url": line.product_url, "comment": line.comment, "requirements": line.requirements} for line in estimate.lines.all()]
    initial_analysis = posted_analysis if posted_analysis is not None else (estimate.document_analysis if estimate else {})
    users = get_user_model().objects.filter(is_active=True).order_by("last_name", "first_name", "username") if request.user.is_superuser else None
    knowledge_sources = []
    if request.user.is_superuser:
        knowledge_sources = list(TenderKnowledgeSource.objects.filter(is_active=True).values("id", "title", "supplier_name", "source_type", "url")[:100])
    source_tender = estimate.tender if pipeline and estimate else None
    saved_estimates, kind_filter, worklist_filter = _saved_estimates_for(request) if shell != "pipeline" else ([], "tender", "active")

    risk_summary = _risk_summary_for(source_tender) if shell == "worklist" else None
    return render(request, "tenders/home.html", {"estimate": estimate, "source_tender": source_tender, "shell": shell, "risk_summary": risk_summary, "saved_estimates": saved_estimates, "kind_filter": kind_filter, "worklist_filter": worklist_filter, "form_state": form_state, "initial_lines_json": json.dumps(initial_lines, ensure_ascii=False), "initial_analysis_json": json.dumps(initial_analysis, ensure_ascii=False), "knowledge_sources_json": json.dumps(knowledge_sources, ensure_ascii=False), "vat_rate": settings.vat_rate, "auto_start_product_search": settings.auto_start_product_search, "auto_recalculate_requirements": settings.auto_recalculate_requirements, "users": users, "is_superuser": request.user.is_superuser, "pipeline": pipeline, "estimate_route": route_prefix, "duplicate_route": f"{route_prefix}_duplicate", "delete_route": f"{route_prefix}_delete", "save_url": reverse(f"{route_prefix}_save", args=[estimate.pk]) if estimate else reverse(f"{route_prefix}_create")})


@login_required
@require_POST
def save_estimate(request, pk=None, pipeline=False):
    """Autosave: the same write as the form POST, but returns JSON and
    never redirects — the tender page keeps its state (and its open ТЗ
    drawer / running AI passes)."""
    settings = TenderSettings.objects.get_or_create(pk=1)[0]
    model = TenderEstimate if pipeline else OrderEstimate
    estimate = _estimate_for_user(request, pk, model) if pk else None
    estimate, error, meta = _persist_tender_estimate(request, estimate, settings, pipeline=pipeline)
    if error:
        return JsonResponse({"error": error}, status=400)
    return JsonResponse({
        "pk": estimate.pk,
        "url": reverse("tender_pipeline_estimate" if pipeline else "tender_estimate", args=[estimate.pk]),
        "line_ids": list(estimate.lines.order_by("sort_order", "pk").values_list("pk", flat=True)),
        "saved_at": timezone.localtime(estimate.updated_at).strftime("%H:%M"),
        "incomplete": meta["incomplete"],
    })


@login_required
@require_POST
def duplicate_estimate(request, pk, pipeline=False):
    model = TenderEstimate if pipeline else OrderEstimate
    line_model = TenderLine if pipeline else OrderLine
    original = _estimate_for_user(request, pk, model)
    lines = list(original.lines.all())
    name = f"{original.name} (копия)"[:300]
    copy = model.objects.create(
        owner=request.user,
        **({"tender": original.tender} if pipeline else {"order": Order.objects.create(name=name)}),
        tender_number=original.tender_number,
        name=name,
        reduction_percent=original.reduction_percent,
        russia_delivery=original.russia_delivery,
        result_notes="",
        vat_rate_snapshot=original.vat_rate_snapshot,
        summary_snapshot=original.summary_snapshot,
        document_analysis=original.document_analysis,
    )
    line_model.objects.bulk_create([
        line_model(
            estimate=copy, sort_order=line.sort_order, name=line.name, quantity=line.quantity,
            nmck_unit=line.nmck_unit, material_unit=line.material_unit,
            application_unit=line.application_unit, logistics_unit=line.logistics_unit,
            product_url=line.product_url, comment=line.comment, requirements=line.requirements,
        )
        for line in lines
    ])
    messages.success(request, f"Создана копия просчёта № {original.tender_number}.")
    return redirect("tender_pipeline_estimate" if pipeline else "tender_estimate", pk=copy.pk)


@login_required
@require_POST
def delete_estimate(request, pk, pipeline=False):
    estimate = _estimate_for_user(request, pk, TenderEstimate if pipeline else OrderEstimate)
    estimate.delete()
    messages.success(request, "Расчёт удалён.")
    return redirect("tender_pipeline_home" if pipeline else "tender_home")


@login_required
@require_POST
def update_estimate_status(request, pk, pipeline=True):
    from tender_selection.models import Tender

    estimate = _estimate_for_user(request, pk, TenderEstimate)
    status = request.POST.get("status", "")
    if status not in dict(Tender.OUTCOME_STATUS_CHOICES) or not estimate.tender_id:
        return HttpResponse(status=400)
    if status == Tender.OUTCOME_PENDING and (estimate.summary_snapshot or {}).get("is_incomplete", True):
        return HttpResponse("Завершите расчёт перед переходом в торги.", status=400)
    tender = estimate.tender
    tender.outcome_status = status
    update_fields = ["outcome_status"]
    # «Архивировать — невыгодно» — тоже терминальное решение по этому тендеру
    # (участвовать не будем), уходит в архив сразу же, как и настоящий факт
    # торгов (см. apply_tender_outcome) — не нужно отдельно жать «Скрыть».
    if status == Tender.OUTCOME_NOT_PARTICIPATED:
        tender.status = Tender.DISMISSED
        tender.archived_at = timezone.now()
        update_fields += ["status", "archived_at"]
    tender.save(update_fields=update_fields)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({
            "status": status,
            "label": tender.get_outcome_status_display(),
            "requires_result": status in {Tender.OUTCOME_LOST, Tender.OUTCOME_WON},
        })
    # Кнопка теперь живёт на странице тендера (не здесь) — туда и возвращаем.
    return redirect("tender_selection:detail", pk=estimate.tender_id)


@login_required
@require_POST
def update_order_status(request, pk):
    estimate = _estimate_for_user(request, pk, OrderEstimate)
    status = request.POST.get("status", "")
    if status not in dict(Order.STATUS_CHOICES):
        return HttpResponse(status=400)
    if not estimate.order_id:
        estimate.order = Order.objects.create(name=estimate.name)
        estimate.save(update_fields=["order"])
    order = estimate.order
    order.status = status
    order.save(update_fields=["status", "updated_at"])
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"status": status, "label": order.get_status_display()})
    return redirect("tender_home")
