from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .documents import DocumentError, extract_preview, fetch_document
from .filtering import match_title, parse_terms
from .models import DocumentPreview, FilterSettings, FoundTender, Organization, PullRun
from .notification import parse_clarifications, parse_complaints, parse_notification
from .regions import REGION_NAMES, region_name
from .services import (
    CATEGORY_GROUPS, effective_laws, effective_okpd2, enrich_one_org,
    extras_for, notification_for, push_to_estimate, run_pull,
)
from .stats import price_stats_for

SORTS = {
    "new": F("published_at").desc(nulls_last=True),
    "old": F("published_at").asc(nulls_last=True),
    "deadline": F("collecting_finished_at").asc(nulls_last=True),
    "deadline_far": F("collecting_finished_at").desc(nulls_last=True),
    "price_hi": F("max_price").desc(nulls_last=True),
    "price_lo": F("max_price").asc(nulls_last=True),
}
DEFAULT_SORT = "deadline"
LAW_LABELS = dict(FoundTender.LAW_CHOICES)


def superuser_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return login_required(wrapped)


@superuser_required
def tender_list(request):
    settings = FilterSettings.load()
    include = parse_terms(settings.include_words)
    exclude = parse_terms(settings.exclude_words)
    show_all = request.GET.get("all") == "1"
    sort = request.GET.get("sort") if request.GET.get("sort") in SORTS else DEFAULT_SORT
    law_filter = request.GET.get("law") if request.GET.get("law") in ("fz44", "fz223") else "all"
    now = timezone.now()
    soon_cutoff = now + timedelta(days=1)

    queryset = FoundTender.objects.exclude(status=FoundTender.DISMISSED)
    if law_filter != "all":
        queryset = queryset.filter(law=law_filter)
    if settings.min_price:
        queryset = queryset.filter(Q(max_price__gte=settings.min_price) | Q(max_price__isnull=True))
    expired = 0
    if not show_all:
        expired = queryset.filter(collecting_finished_at__lt=now).count()
        queryset = queryset.filter(Q(collecting_finished_at__gte=now) | Q(collecting_finished_at__isnull=True))
    queryset = queryset.order_by(SORTS[sort], F("first_seen_at").desc())

    rows, hidden = [], 0
    for tender in queryset:
        passes, hits = match_title(tender.title or tender.object_info, include, exclude)
        if passes or show_all:
            tender.match_hits = hits
            tender.filtered_out = not passes
            rows.append(tender)
        else:
            hidden += 1

    page = Paginator(rows, 100).get_page(request.GET.get("page"))
    orgs = {o.inn: o for o in Organization.objects.filter(
        inn__in=[t.customer_inn for t in page.object_list if t.customer_inn]
    )}
    for tender in page.object_list:
        tender.org = orgs.get(tender.customer_inn)
        tender.region_label = region_name(tender.region) if tender.region else ""
        tender.law_label = LAW_LABELS.get(tender.law, tender.law)
        tender.is_soon = bool(tender.collecting_finished_at and now <= tender.collecting_finished_at <= soon_cutoff)
        tender.on_estimate = tender.status == FoundTender.PUSHED and tender.pushed_estimate_id
        tender.has_complaint = bool(tender.complaints_raw)

    counts = dict(FoundTender.objects.exclude(status=FoundTender.DISMISSED).values_list("law").annotate(n=Count("law")))
    return render(request, "tender_selection/list.html", {
        "page_obj": page,
        "shown_count": len(rows),
        "hidden_count": hidden,
        "expired_count": expired,
        "show_all": show_all,
        "sort": sort,
        "law_filter": law_filter,
        "law_counts": {"all": sum(counts.values()), "fz44": counts.get("fz44", 0), "fz223": counts.get("fz223", 0)},
        "settings": settings,
        "last_run": PullRun.objects.first(),
    })


@superuser_required
def tender_detail(request, pk):
    tender = get_object_or_404(FoundTender, pk=pk)
    payload = notification_for(tender, force=request.GET.get("refresh") == "1")
    card = parse_notification(payload) if payload else None
    estimate_id = tender.pushed_estimate_id if tender.status == FoundTender.PUSHED else None

    org = Organization.objects.filter(inn=tender.customer_inn).first() if tender.customer_inn else None
    if tender.law != "fz44" and org is None and tender.customer_inn:
        org = enrich_one_org(tender.customer_inn, tender.law)  # для 223 карточки заказчика больше неоткуда взять

    clar_raw, comp_raw = extras_for(tender, force=request.GET.get("refresh") == "1")

    stats = price_stats_for(tender, card)
    if stats:
        for row in stats["examples"]:
            row.region_label = region_name(row.region) if row.region else ""

    return render(request, "tender_selection/detail.html", {
        "tender": tender,
        "card": card,
        "org": org,
        "region_label": region_name(tender.region) if tender.region else "",
        "fetch_failed": payload is None and tender.law == "fz44",
        "is_fz223": tender.law != "fz44",
        "pushed_estimate_id": estimate_id,
        "clarifications": parse_clarifications(clar_raw),
        "complaints": parse_complaints(comp_raw),
        "price_stats": stats,
    })


@superuser_required
def doc_preview(request, pk, idx):
    tender = get_object_or_404(FoundTender, pk=pk)
    card = parse_notification(tender.notification_raw) if tender.notification_raw else None
    docs = (card or {}).get("documents", [])
    if not 0 <= idx < len(docs):
        return JsonResponse({"error": "Документ не найден."}, status=404)
    doc = docs[idx]
    url, name = doc.get("url", ""), doc.get("name", "")

    cached = DocumentPreview.objects.filter(url=url).first()
    if cached and request.GET.get("refresh") != "1":
        return JsonResponse({"name": name, "kind": cached.kind, "html": cached.html, "error": cached.error})

    try:
        data = fetch_document(url)
    except DocumentError as exc:
        # сетевые сбои не кэшируем — на проде повтор может пройти
        return JsonResponse({"name": name, "kind": "", "html": "", "error": str(exc)})

    result = extract_preview(data, name)
    DocumentPreview.objects.update_or_create(url=url, defaults={
        "filename": name, "kind": result.get("kind", ""),
        "html": result.get("html", ""), "error": result.get("error", ""),
    })
    return JsonResponse({"name": name, "kind": result.get("kind", ""),
                         "html": result.get("html", ""), "error": result.get("error", "")})


@superuser_required
def filter_settings(request):
    settings = FilterSettings.load()
    if request.method == "POST":
        settings.include_words = request.POST.get("include_words", "").strip()
        settings.exclude_words = request.POST.get("exclude_words", "").strip()
        settings.min_price = request.POST.get("min_price") or 0
        settings.window_days = request.POST.get("window_days") or 7
        settings.okpd2_codes = request.POST.getlist("okpd2")
        settings.regions = [r for r in request.POST.getlist("region") if r.isdigit()]
        settings.laws = [law for law in request.POST.getlist("law") if law in ("fz44", "fz223")] or ["fz44"]
        settings.save()
        messages.success(request, "Настройки подбора сохранены.")
        return redirect("tender_selection:list")

    chosen_codes = set(effective_okpd2(settings))
    chosen_regions = set(str(r) for r in (settings.regions or []))
    chosen_laws = set(effective_laws(settings))
    return render(request, "tender_selection/settings.html", {
        "settings": settings,
        "categories": [(code, label, code in chosen_codes) for code, label in CATEGORY_GROUPS],
        "regions": [(code, name, str(code) in chosen_regions) for code, name in sorted(REGION_NAMES.items(), key=lambda x: x[1])],
        "laws": [(code, label, code in chosen_laws) for code, label in FoundTender.LAW_CHOICES],
        "using_defaults": not settings.okpd2_codes,
    })


@superuser_required
@require_POST
def pull_now(request):
    run = run_pull(max_requests=16)
    if run.ok:
        messages.success(
            request,
            f"Выгрузка: запросов {run.requests_made}, записей {run.records_received}, "
            f"новых {run.created_count}, за {run.duration_seconds} сек.",
        )
    else:
        messages.error(request, f"Выгрузка не удалась: {run.error}")
    return redirect("tender_selection:list")


@superuser_required
@require_POST
def push_estimate(request, pk):
    tender = get_object_or_404(FoundTender, pk=pk)
    if tender.status == FoundTender.PUSHED and tender.pushed_estimate_id:
        return redirect("tender_estimate", pk=tender.pushed_estimate_id)
    try:
        estimate_id = push_to_estimate(tender, request.user)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось создать просчёт: {exc}")
        return redirect("tender_selection:detail", pk=pk)
    messages.success(request, "Просчёт создан. Позиции подставлены из извещения.")
    return redirect("tender_estimate", pk=estimate_id)


@superuser_required
@require_POST
def dismiss(request, pk):
    tender = get_object_or_404(FoundTender, pk=pk)
    tender.status = FoundTender.DISMISSED
    tender.save(update_fields=["status"])
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")


@superuser_required
@require_POST
def set_review(request, pk):
    tender = get_object_or_404(FoundTender, pk=pk)
    value = request.POST.get("review", "")
    if value in dict(FoundTender.REVIEW_CHOICES):
        tender.review = value
        tender.save(update_fields=["review"])
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"review": tender.review, "label": tender.get_review_display()})
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")
