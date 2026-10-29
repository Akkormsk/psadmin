from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.cache import never_cache

from .documents import DocumentError, extract_preview, extract_zip_entry
from .models import ContractStat, FilterSettings, Organization, PullRun, Tender, TenderDismissalFeedback
from .notification import parse_clarifications, parse_complaints, parse_notification
from .regions import REGION_NAMES, region_name
from .services import (
    CATEGORY_GROUPS, _fetch_doc_bytes, preview_tender_document, reduction_percent_from, effective_laws, effective_okpd2, enrich_one_org,
    extras_for, extras_need_refresh, notification_for, archive_tender, archive_stage_for, risk_assessment_for,
    start_extras_refresh_in_background, EVALUATION_NUMBERS, RISK_FACTOR_LABELS, RISK_LEVEL_CHOICES,
    _TENDER_SETTINGS_FIELDS, evaluation_settings_form, save_evaluation_settings,
    change_forecast_inclusion, create_tender_estimate, hide_tender, restore_hidden_tender, save_tender_bid,
    update_tender_review, apply_filter_word_audit, pull_tenders_now, record_tender_outcome, run_filter_word_audit, run_profile_triage,
    decide_document_proposals, upload_tender_document_preview, visible_found_tenders,
)
from .protocols import ProtocolError, find_ours
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
LAW_LABELS = dict(Tender.LAW_CHOICES)


def _dismiss_redirect(request):
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
        return redirect(next_url)
    return redirect("tender_selection:list")


def superuser_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return login_required(wrapped)


def _tender_viewable_by(user, tender):
    """Суперюзер видит любой тендер. Менеджер — только тот, что стал ЕГО просчётом
    (та же проверка владения, что и у самого просчёта — см. _estimate_for_user в
    tenders/views.py): попасть можно по ссылке «Открыть карточку →» со страницы
    своего просчёта, а не подбором номера в адресной строке и не через каталог."""
    if user.is_superuser:
        return True
    if tender.status != Tender.PUSHED:
        return False
    from tenders.models import TenderEstimate

    return tender.estimates.filter(owner=user).exists()


def tender_viewer_required(view):
    """Весь lifecycle тендера доступен только администратору."""
    return superuser_required(view)


def _risk_badge(tender) -> dict:
    """Светофор по итоговому уровню оценки, а пока её нет — её состояние."""
    if tender.risk_error:
        return {"state": "error", "text": "риск: ошибка"}
    if not tender.risk_checked_at or "risk_factors" not in (tender.risk_assessment or {}):
        return {"state": "pending", "text": "риск: ожидает"}
    return {
        "low": {"state": "ok", "text": "риск: низкий"},
        "medium": {"state": "warn", "text": "риск: средний"},
        "high": {"state": "error", "text": "риск: высокий"},
    }.get((tender.risk_assessment or {}).get("risk_level"), {"state": "ok", "text": "риск: оценён"})


def _procurement_status(tender, card=None) -> dict:
    """Понятная стадия самой закупки, независимо от нашей воронки."""
    now = timezone.localtime()
    deadline = tender.collecting_finished_at
    if deadline and now <= deadline:
        return {
            "label": "Подача заявок", "badge_state": "pending", "summary_state": "neutral",
            "detail": f"Приём заявок открыт до {timezone.localtime(deadline):%d.%m.%Y %H:%M} МСК.",
        }

    if card is None:
        card = parse_notification(tender.notification_raw) if tender.notification_raw else None
    summarizing = (card or {}).get("dates", {}).get("summarizing")
    if summarizing:
        result_day = timezone.localtime(summarizing).date()
        if result_day == now.date():
            return {
                "label": "Подведение итогов сегодня", "badge_state": "warn", "summary_state": "warning",
                "detail": "Сегодня по срокам извещения должно состояться подведение итогов.",
            }
        if result_day < now.date():
            return {
                "label": "Ожидаем протокол", "badge_state": "error", "summary_state": "negative",
                "detail": f"Подведение итогов было {result_day:%d.%m.%Y}; протокол в ЕИС пока не найден.",
            }
        return {
            "label": "Рассмотрение заявок", "badge_state": "warn", "summary_state": "warning",
            "detail": f"Приём заявок завершён. Подведение итогов — {result_day:%d.%m.%Y}.",
        }

    if deadline:
        return {
            "label": "Рассмотрение заявок", "badge_state": "warn", "summary_state": "warning",
            "detail": "Приём заявок завершён; ожидаем публикацию протокола в ЕИС.",
        }
    return {
        "label": "Статус уточняется", "badge_state": "pending", "summary_state": "neutral",
        "detail": "В извещении пока нет срока подачи заявок.",
    }


def _auction_event(card) -> dict | None:
    dates = (card or {}).get("dates", {})
    bidding_at = dates.get("bidding")
    if not bidding_at:
        return None

    now = timezone.localtime()
    bidding_at = timezone.localtime(bidding_at)
    has_time = dates.get("bidding_has_time", False)
    if (has_time and bidding_at <= now) or (not has_time and bidding_at.date() < now.date()):
        return None

    if has_time:
        label = f"Аукцион {bidding_at:%d.%m} · {bidding_at:%H:%M}"
        state = "error" if bidding_at - now <= timedelta(hours=2) else "warn" if bidding_at.date() == now.date() else "info"
        detail = f"Электронный аукцион {bidding_at:%d.%m.%Y в %H:%M} МСК"
    else:
        label = f"Аукцион {bidding_at:%d.%m}"
        state = "warn" if bidding_at.date() == now.date() else "info"
        detail = f"Электронный аукцион {bidding_at:%d.%m.%Y}; время площадка не передала"

    etp = (card or {}).get("etp", {})
    return {"label": label, "state": state, "detail": detail, "url": etp.get("url", ""), "etp_name": etp.get("name", "")}


def _market_forecast_comparison(tender) -> dict | None:
    if tender.market_forecast_percent is None or tender.contract_reduction_percent is None:
        return None
    delta = tender.contract_reduction_percent - tender.market_forecast_percent
    return {
        "forecast": tender.market_forecast_percent,
        "actual": tender.contract_reduction_percent,
        "delta": abs(delta),
        "direction": "выше" if delta > 0 else "ниже" if delta < 0 else "exact",
        "sample_count": tender.market_forecast_sample_count,
    }


def _rejection_explanation(protocol) -> str:
    reason = ((protocol or {}).get("ours") or {}).get("reject_reason", "")
    if "ст. 14" not in reason.lower():
        return ""
    return (
        "Простыми словами: заявка не подтвердила соответствие правилам национального режима — "
        "требованиям к происхождению товара и документам, которые это подтверждают. "
        "На практике для таких закупок обычно применяют Постановление Правительства № 1875: "
        "для отдельных товаров нужны страна происхождения и, когда это указано в извещении, "
        "номер записи в реестре российской промышленной продукции или реестре ЕАЭС. "
        "Протокол не уточняет, какого именно подтверждения не хватило — это нужно сверить с извещением."
    )


def _found_tender_card(tender):
    """Карточка «Входящие»/«Проверка» — тендер ещё не отправлен в расчёт.

    Решение «вперёд» (На оценку рисков / В расчёт) принимается только внутри
    самого тендера, не с карточки — рано жать кнопку, не открыв, что внутри.
    На карточке остаётся лишь маленький «×» скрыть (по ховеру).

    Оценка риска не показывается, пока тендер не прошёл «Входящие» — на
    этой стадии её ещё не считали (расчёт запускается при открытии карточки
    на стадии «Проверка»), нечего показывать раньше времени."""
    reviewed = tender.review != Tender.UNREVIEWED
    status_key = "error" if tender.risk_error else "assessed" if tender.risk_checked_at else "new"
    badges = [_risk_badge(tender)] if reviewed else []
    now = timezone.now()
    is_soon = bool(tender.collecting_finished_at and now <= tender.collecting_finished_at <= now + timedelta(days=1))
    org = getattr(tender, "org", None)
    customer = (org.name if org and org.name else "") or (f"ИНН {tender.customer_inn}" if tender.customer_inn else "")
    return {
        "kind": "found",
        "pk": tender.pk,
        "title": tender.title or tender.object_info,
        "customer": customer,
        "law_label": tender.get_law_display(),
        "purchase_number": tender.purchase_number,
        "max_price": tender.max_price,
        "deadline": tender.collecting_finished_at,
        "is_soon": is_soon,
        "status_label": "",
        "status_key": status_key,
        "badges": badges,
        "detail_url": f"{reverse('tender_selection:detail', args=[tender.pk])}?return_to=board",
        "dismiss_url": reverse("tender_selection:dismiss", args=[tender.pk]),
    }


def _estimate_card(estimate):
    """Карточка «Расчёт»/«Торги»/«Результат» — просчёт, откуда бы он ни пришёл
    (перенесён из подбора или создан вручную импортом в самих «Тендерах»).

    Бейджи накапливаются по мере продвижения, не заменяют друг друга и идут в
    порядке стадий: риск с «Оценки», ROI с «Расчёта», «Выигран»/«Проигран» с «Результата»."""
    summary = estimate.summary_snapshot or {}
    badges = []
    # Расчёт есть — «Оценка» пройдена, даже если тендер перенесён по старой схеме без отметки «в работу».
    if estimate.tender_id:
        badges.append(_risk_badge(estimate.tender))
    if summary.get("roi") is not None:
        from decimal import Decimal, InvalidOperation

        from tenders.services import roi_thresholds

        if summary.get("is_incomplete", True):
            # Себестоимость ещё не досчитана до конца — ROI не окончательный, не красим.
            roi_state = "pending"
        else:
            try:
                good, thin = roi_thresholds()
                roi_value = Decimal(str(summary["roi"]))
                roi_state = "ok" if roi_value >= good else "warn" if roi_value >= thin else "error"
            except InvalidOperation:
                roi_state = "pending"
        badges.append({"state": roi_state, "text": f"ROI {summary['roi']}%"})
    tender = estimate.tender
    outcome_status = tender.outcome_status if tender else Tender.OUTCOME_DRAFT
    card = parse_notification(tender.notification_raw) if tender and tender.notification_raw else None
    procurement_status = _procurement_status(tender, card) if tender and outcome_status == Tender.OUTCOME_PENDING else None
    auction_event = _auction_event(card) if procurement_status else None
    if procurement_status:
        badges.insert(0, {"state": procurement_status["badge_state"], "text": procurement_status["label"]})
    if auction_event:
        badges.insert(1, {"state": auction_event["state"], "text": auction_event["label"]})
    if tender and tender.outcome_status == Tender.OUTCOME_WON:
        badges.append({"state": "ok", "text": "Выигран"})
    elif tender and tender.outcome_status == Tender.OUTCOME_LOST:
        ours = find_ours(tender.protocol or {}, bid_number=tender.bid_number, bid_price=tender.bid_price)
        badges.append({"state": "error", "text": "Отклонена" if ours and ours.get("rejected") else "Проигран"})
    # Карточка ведёт на страницу ТЕНДЕРА (с растущими блоками по стадиям), а не
    # сразу в рабочее пространство расчёта — туда только через кнопку «Перейти
    # в расчёт» внутри блока «Расчёт» на самой странице тендера.
    detail_url = f"{reverse('tender_selection:detail', args=[estimate.tender_id])}?return_to=board" if estimate.tender_id else ""
    return {
        "kind": "estimate",
        "pk": estimate.pk,
        "title": (tender.title or tender.object_info) if tender else estimate.name,
        "customer": (
            (getattr(tender, "org", None).name if getattr(tender, "org", None) and tender.org.name else "")
            or (f"ИНН {tender.customer_inn}" if tender and tender.customer_inn else "")
        ),
        "purchase_number": tender.purchase_number if tender else estimate.tender_number,
        "tender_number": tender.purchase_number if tender else estimate.tender_number,
        "max_price": tender.max_price if tender else None,
        "deadline": tender.collecting_finished_at if tender else None,
        "is_soon": bool(
            tender and tender.collecting_finished_at
            and timezone.now() <= tender.collecting_finished_at <= timezone.now() + timedelta(days=1)
        ),
        "status": outcome_status,
        "status_label": tender.get_outcome_status_display() if tender else "",
        "status_key": outcome_status,
        "procurement_status": procurement_status,
        "auction_event": auction_event,
        "badges": badges,
        # Внесение итога живёт на странице самого тендера (tenders/home.html,
        # рядом с прогнозом снижения), не на карточке канбана — здесь только
        # уже накопленный результат в badges выше.
        "detail_url": detail_url,
        "dismiss_url": reverse("tender_selection:dismiss_estimate", args=[estimate.pk]),
        # Архивировать «в тихую» просчёт, по которому ещё не внесён итог торгов, —
        # частая случайная потеря данных; предупреждаем перед этим (см. kanban.html).
        "warn_before_dismiss": not (tender.outcome_checked_at if tender else False),
    }


_KANBAN_COLUMN_KEYS = ("review", "calculation", "bidding", "result")


def _kanban_column_dirs(request):
    """Направление сортировки каждого столбца канбана — независимо друг от
    друга, из query-параметров ``dir_<key>``. По умолчанию — новые сверху."""
    return {key: ("asc" if request.GET.get(f"dir_{key}") == "asc" else "desc") for key in _KANBAN_COLUMN_KEYS}


def _kanban_toggle_qs(dirs, key):
    """Ссылка-стрелка одного столбца: та же сортировка у остальных, у этого — наоборот."""
    from urllib.parse import urlencode
    flipped = dict(dirs, **{key: "asc" if dirs[key] == "desc" else "desc"})
    return urlencode({f"dir_{k}": v for k, v in flipped.items() if v == "asc"})


def kanban(request):
    """Единая доска жизненного цикла тендера — не новая сущность, а объединённое
    чтение Tender (ещё не в расчёте) и TenderEstimate (расчёт, из любого
    источника: перенос из подбора или ручной импорт) в одном списке карточек."""
    dirs = _kanban_column_dirs(request)
    settings = FilterSettings.load()

    def _deadline_order(dir_key, deadline_field, tie_breaker):
        if dirs[dir_key] == "asc":
            return (F(deadline_field).desc(nulls_last=True), F(tie_breaker).asc())
        return (F(deadline_field).asc(nulls_last=True), F(tie_breaker).desc())

    def _visible(base_qs, dir_key):
        qs = base_qs.order_by(*_deadline_order(dir_key, "collecting_finished_at", "first_seen_at"))
        rows, _hidden, _expired = visible_found_tenders(
            qs, min_price=settings.min_price,
            include_words=settings.include_words, exclude_words=settings.exclude_words,
        )
        return rows

    # «Входящие» больше не колонка канбана — это отдельный список (tender_list);
    # сюда тендер попадает только после «В работу» (review != unreviewed).
    review_tenders = _visible(Tender.objects.filter(status=Tender.NEW).exclude(review=Tender.UNREVIEWED), "review")
    orgs = {o.inn: o for o in Organization.objects.filter(inn__in={t.customer_inn for t in review_tenders if t.customer_inn})}
    for t in review_tenders:
        t.org = orgs.get(t.customer_inn)
    review = [
        _found_tender_card(t) for t in review_tenders
    ]

    from tenders.models import TenderEstimate

    live_estimates = TenderEstimate.objects.filter(
        tender__isnull=False,
    ).exclude(tender__status=Tender.DISMISSED).select_related("tender")
    estimate_orgs = {
        org.inn: org for org in Organization.objects.filter(
            inn__in=live_estimates.values_list("tender__customer_inn", flat=True),
        )
    }

    def estimate_card(estimate):
        estimate.tender.org = estimate_orgs.get(estimate.tender.customer_inn)
        return _estimate_card(estimate)

    calculation = [
        estimate_card(e) for e in
        live_estimates.filter(tender__outcome_status=Tender.OUTCOME_DRAFT).order_by(
            *_deadline_order("calculation", "tender__collecting_finished_at", "updated_at")
        )
    ]
    bidding = [
        estimate_card(e) for e in
        live_estimates.filter(tender__outcome_status=Tender.OUTCOME_PENDING).order_by(
            *_deadline_order("bidding", "tender__collecting_finished_at", "updated_at")
        )
    ]
    result = [
        estimate_card(e) for e in
        live_estimates.exclude(tender__outcome_status__in=(Tender.OUTCOME_DRAFT, Tender.OUTCOME_PENDING))
        .order_by(*_deadline_order("result", "tender__collecting_finished_at", "updated_at"))
    ]

    columns = [
        {"key": "review", "label": "Оценка", "cards": review},
        {"key": "calculation", "label": "Расчёт", "cards": calculation},
        {"key": "bidding", "label": "Торги", "cards": bidding},
        {"key": "result", "label": "Результат", "cards": result},
    ]
    for column in columns:
        column["dir"] = dirs[column["key"]]
        column["toggle_qs"] = _kanban_toggle_qs(dirs, column["key"])
    archived_count = (
        Tender.objects.filter(status=Tender.DISMISSED).count()
    )
    return render(request, "tender_selection/kanban.html", {
        "columns": columns, "archived_count": archived_count, "settings": settings,
        "nav_counts": {"incoming": _incoming_count(settings), "board": sum(len(c["cards"]) for c in columns)},
    })


def _deadline_urgency(deadline, now) -> tuple[int | None, str]:
    """(полных дней до окончания подачи, 'urgent' — сутки (последний день) | 'soon' — двое суток | '')."""
    if deadline is None or deadline < now:
        return None, ""
    days = (deadline - now).days
    return days, "urgent" if days <= 0 else "soon" if days == 1 else ""


def _incoming_count(settings) -> int:
    rows, _hidden, _expired = visible_found_tenders(
        Tender.objects.filter(status=Tender.NEW, review=Tender.UNREVIEWED), min_price=settings.min_price,
        include_words=settings.include_words, exclude_words=settings.exclude_words,
    )
    return len(rows)


def _board_count(settings) -> int:
    """Столько же карточек, сколько на доске «Торги» (см. kanban)."""
    from tenders.models import TenderEstimate

    review, _hidden, _expired = visible_found_tenders(
        Tender.objects.filter(status=Tender.NEW).exclude(review=Tender.UNREVIEWED), min_price=settings.min_price,
        include_words=settings.include_words, exclude_words=settings.exclude_words,
    )
    estimates = TenderEstimate.objects.filter(tender__isnull=False).exclude(tender__status=Tender.DISMISSED).count()
    return len(review) + estimates


@superuser_required
def tender_list(request):
    """«Входящие» — только первичный отбор входящего потока: пока тендер не
    переведён «В работу» (review=unreviewed). Жизненным циклом уже отобранных
    занимается канбан (kanban()), не этот экран — сюда попавшие «в работу»
    не возвращаются, и статус тут показывать нечего."""
    view = request.GET.get("view")
    if view is None:
        # Голый заход без ?view= — открываем ту вкладку, что смотрели в
        # прошлый раз в этой сессии, а не всегда канбан («Торги»).
        view = request.session.get("ts_last_view", "kanban")
    request.session["ts_last_view"] = "list" if view == "list" else "kanban"
    if view != "list":
        return kanban(request)

    settings = FilterSettings.load()
    show_all = request.GET.get("all") == "1"
    sort = request.GET.get("sort") if request.GET.get("sort") in SORTS else DEFAULT_SORT
    law_filter = request.GET.get("law") if request.GET.get("law") in ("fz44", "fz223") else "all"
    query = request.GET.get("q", "").strip()
    now = timezone.now()

    queryset = Tender.objects.filter(status=Tender.NEW, review=Tender.UNREVIEWED).defer(
        "raw", "clarifications_raw", "risk_assessment", "risk_assessment_docs",
    )
    if law_filter != "all":
        queryset = queryset.filter(law=law_filter)
    queryset = queryset.order_by(SORTS[sort], F("first_seen_at").desc())

    rows, hidden, expired = visible_found_tenders(
        queryset, min_price=settings.min_price, include_words=settings.include_words,
        exclude_words=settings.exclude_words, show_all=show_all,
    )

    orgs = {o.inn: o for o in Organization.objects.filter(inn__in={t.customer_inn for t in rows if t.customer_inn})}
    for tender in rows:
        tender.org = orgs.get(tender.customer_inn)
    if query:
        needle = query.casefold()
        rows = [t for t in rows if needle in " ".join([
            t.title, t.object_info, t.purchase_number, t.customer_inn, t.org.name if t.org else "",
        ]).casefold()]

    page = Paginator(rows, 100).get_page(request.GET.get("page"))
    from tenders.analysis_presentation import resolve_tender_analysis_statuses
    presentations = resolve_tender_analysis_statuses(page.object_list)
    for tender in page.object_list:
        tender.analysis_status = presentations[tender.pk]
        tender.region_label = region_name(tender.region) if tender.region else ""
        tender.law_label = LAW_LABELS.get(tender.law, tender.law)
        tender.days_left, tender.deadline_state = _deadline_urgency(tender.collecting_finished_at, now)
        tender.has_complaint = bool(tender.complaints_raw)
        # 223-ФЗ не имеет разобранного извещения по конструкции источника — это не
        # ошибка. У 44-ФЗ пустой notification_raw значит запрос ещё не удался — но
        # только если попытка вообще была (notification_checked_at): свежевыгруженный
        # тендер, который ещё никто не открывал и фон (retry_pending_notifications)
        # до него не добрался, — это не сбой, а «ещё не проверяли», значок не должен
        # гореть на буквально каждой новой карточке.
        tender.notification_missing = (
            tender.law == "fz44" and not tender.notification_raw and tender.notification_checked_at is not None
            and not tender.notification_error
        )

    counts = dict(
        Tender.objects.filter(status=Tender.NEW, review=Tender.UNREVIEWED)
        .values_list("law").annotate(n=Count("law"))
    )
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
        "query": query,
        "nav_counts": {
            "incoming": sum(
                not tender.filtered_out and (
                    tender.collecting_finished_at is None or tender.collecting_finished_at >= now
                ) for tender in rows
            ),
            "board": _board_count(settings),
        },
    })


@tender_viewer_required
def tender_detail(request, pk):
    tender = get_object_or_404(Tender, pk=pk)
    is_archived = tender.status == Tender.DISMISSED
    return_url = (
        reverse("tender_selection:archive") if is_archived or request.GET.get("from") == "archive"
        else f"{reverse('tender_selection:list')}?view=kanban" if request.GET.get("return_to") == "board"
        else reverse("tender_selection:list")
    )
    analysis_snapshot = request.GET.get("analysis_snapshot") == "1"
    if tender.opened_at is None and not analysis_snapshot:  # для «жирного» непрочитанного в списке — только реальный заход, не фон
        tender.opened_at = timezone.now()
        tender.save(update_fields=["opened_at"])
    is_manual = tender.source == Tender.MANUAL
    payload = (tender.notification_raw or None) if analysis_snapshot else None if is_manual else notification_for(tender, force=request.GET.get("refresh") == "1")
    card = parse_notification(payload) if payload else None
    estimate_id = tender.estimates.order_by("-updated_at").values_list("pk", flat=True).first()
    from tenders.models import OwnerInteraction, TenderComputeLine, TenderComputeJob, TenderSourceItem
    from tenders.analysis_presentation import analysis_issues, resolve_tender_analysis_status
    analysis_status = resolve_tender_analysis_status(tender)
    v2_source_items = list(tender.v2_source_items.select_related("parent", "supersedes").order_by("source_type", "source_key", "pk"))
    from tenders.document_enrichment import characteristic_display, item_name_parts, effective_item_requirements, is_item_enrichment, merge_item_requirements, notification_origin, notification_quote_source
    notification_by_index = {index: item for index, item in enumerate((card or {}).get("items", []))}
    source_financials = []
    document_proposals = [item for item in v2_source_items if item.source_type == "document_proposal"]
    document_proposal_groups = []
    proposal_documents = {doc.get("url"): doc for doc in (card or {}).get("documents", [])}
    proposals_by_parent = {}
    for proposal in document_proposals:
        # A decision is made for the complete replacement of one notification
        # position, never for an opaque individual AI row.
        parent_key = proposal.parent_id or proposal.pk
        group = proposals_by_parent.get(parent_key)
        if group is None:
            parent = proposal.parent
            group = {
                "parent": parent,
                "display_name": str((proposal.metadata or {}).get("parent_display_name") or (parent.original_text if parent else "Позиция из документации")),
                "notification_position": (parent.metadata or {}).get("notification_index", -1) + 1 if parent else None,
                "items": [],
                "parent_quantity": parent.quantity if parent else None,
                "parent_unit": parent.unit if parent else "",
                "original_values": notification_by_index.get((notification_origin(parent).metadata or {}).get("notification_index"), {}) if parent and notification_origin(parent) else {},
            }
            proposals_by_parent[parent_key] = group
            document_proposal_groups.append(group)
        requirements = effective_item_requirements(proposal)
        raw_characteristics = requirements.get("characteristics") if isinstance(requirements.get("characteristics"), list) else []
        characteristics = [characteristic_display(row, documents=proposal_documents) for row in raw_characteristics if isinstance(row, dict)]
        if not characteristics:
            characteristics = [
                {"name": str(key), "value": str(value)}
                for key, value in requirements.get("document_requirements", requirements).items()
                if value not in (None, "", [], {})
            ]
        metadata = proposal.metadata if isinstance(proposal.metadata, dict) else {}
        group["items"].append({
            "name": proposal.original_text,
            "name_parts": item_name_parts(proposal),
            "quantity": proposal.quantity,
            "unit": proposal.unit,
            "quantity_per_parent": metadata.get("quantity_per_parent"),
            "relationship": metadata.get("relationship"),
            "quote_eligible": notification_quote_source(proposal) is not None,
            "characteristics": [row for row in characteristics if row.get("scope") != "completeness"],
            "completeness": [row for row in characteristics if row.get("scope") == "completeness"],
            "supply_kind": metadata.get("supply_kind", ""),
            "structure_reason": metadata.get("structure_reason", ""),
            "omitted_facts": requirements.get("omitted_facts", []),
            "review_error": requirements.get("reconciliation_error", ""),
            "provenance": proposal.provenance if isinstance(proposal.provenance, dict) else {},
            "document": proposal_documents.get((proposal.provenance or {}).get("document_url")),
        })
    v2_lines = TenderComputeLine.objects.filter(job__tender=tender).select_related("source_item").order_by("job_id", "pk")
    v2_questions = list(OwnerInteraction.objects.filter(tender=tender, status="open").select_related("source_item", "compute_line").order_by("pk"))
    v2_job = TenderComputeJob.objects.filter(tender=tender, version__startswith="v2").order_by("-pk").first()
    display_items = card.get("items", []) if card else []
    has_document_items = False
    document_item_count = 0
    original_source_texts = []
    if card:
        from tenders.analysis_presentation import document_item_has_change
        from tenders.calculation_v2_pipeline import active_calculation_items

        notification_items = card.get("items", [])
        notification_by_index = {index: item for index, item in enumerate(notification_items)}
        questions_by_source = {question.source_item_id: question for question in v2_questions if question.source_item_id}
        document_names = {document.get("url"): document.get("name", "Документация") for document in card.get("documents", [])}
        active_sources = active_calculation_items(tender) if v2_source_items else []
        financial_parents = set()
        for source in active_sources:
            origin = notification_origin(source)
            if origin and notification_quote_source(source) is None and origin.pk not in financial_parents:
                original = notification_by_index.get((origin.metadata or {}).get("notification_index"), {})
                source_financials.append({**original, "source_item_id": origin.pk, "name": (source.metadata or {}).get("parent_display_name") or origin.original_text})
                financial_parents.add(origin.pk)
        from tenders.models import TenderCommercialItem
        commercial_items = list(tender.v2_commercial_items.prefetch_related("components__source_item", "source_items").filter(status="active").order_by("pk"))
        if commercial_items and not any(source.source_type == "document_extraction" for source in active_sources):
            display_items = []
            for commercial in commercial_items:
                linked_sources = list(commercial.source_items.all())
                source = linked_sources[0] if len(linked_sources) == 1 else None
                requirements = merge_item_requirements(effective_item_requirements(source), commercial.requirements) if source and commercial.structure == TenderCommercialItem.Structure.SIMPLE else commercial.requirements if isinstance(commercial.requirements, dict) else {}
                original_source = notification_origin(source, same_item=True) if source else None
                quote_source = notification_quote_source(source) if source and commercial.structure == TenderCommercialItem.Structure.SIMPLE and commercial.quantity == source.quantity and str(commercial.unit or source.unit).strip().rstrip(".") == str(source.unit).strip().rstrip(".") else None
                notification_item = notification_by_index.get((original_source.metadata or {}).get("notification_index"), {}) if original_source else {}
                if quote_source is None:
                    for linked_source in linked_sources:
                        origin = notification_origin(linked_source)
                        if origin and origin.pk not in financial_parents:
                            source_financials.append({**notification_by_index.get(origin.metadata.get("notification_index"), {}), "name": origin.original_text})
                            financial_parents.add(origin.pk)
                rows = requirements.get("characteristics") if isinstance(requirements.get("characteristics"), list) else []
                details = [characteristic_display(row, documents=proposal_documents) for row in rows if isinstance(row, dict)]
                components = [{"name": component.name, "quantity_per_parent": component.quantity_per_parent, "unit": component.unit} for component in commercial.components.filter(status="active").order_by("sort_order", "pk")]
                changed = commercial.structure != TenderCommercialItem.Structure.SIMPLE or (source and source.source_type in {"document_enrichment", "document_extraction"})
                display_items.append({"name": commercial.display_name, "quantity": commercial.quantity, "unit": commercial.unit, "code": notification_item.get("code", ""), "code_name": notification_item.get("code_name", ""), "price": notification_item.get("price") if quote_source else None, "sum": notification_item.get("sum") if quote_source else None,
                    "characteristics": details, "components": components if commercial.structure == TenderCommercialItem.Structure.COMPOSITE else [],
                    "source_mark": "Уточнено по ТЗ" if changed else "", "source_mark_kind": "enriched", "source_details": "Документация" if changed else "", "source_evidence": "", "source_changes": [], "original_source_text": "",
                    "composition_question": "", "composition_question_id": None, "composition_options": []})
        elif active_sources:
            display_items = []
            for source in active_sources:
                origin = notification_origin(source)
                requirements = effective_item_requirements(source)
                provenance = source.provenance if isinstance(source.provenance, dict) else {}
                question = questions_by_source.get(source.pk)
                is_document_item = source.source_type in {"document_extraction", "document_enrichment"} and document_item_has_change(source)
                raw_characteristics = requirements.get("characteristics") if isinstance(requirements.get("characteristics"), list) else []
                if raw_characteristics:
                    details = [characteristic_display(row, documents=proposal_documents) for row in raw_characteristics if isinstance(row, dict)]
                else:
                    details = [{"name": str(key), "value": str(value)} for key, value in requirements.get("document_requirements", requirements).items() if value not in (None, "", [], {})]
                metadata = source.metadata if isinstance(source.metadata, dict) else {}
                original_source = notification_origin(source, same_item=True)
                quote_source = notification_quote_source(source)
                notification_item = notification_by_index.get((original_source.metadata or {}).get("notification_index"), {}) if original_source else {}
                source_name = document_names.get(provenance.get("document_url"), "Документация")
                source_bits = [source_name, provenance.get("page_or_section")]
                evidence = str(provenance.get("evidence") or "").strip()
                parent_text = source.parent.original_text if source.parent_id and source.parent else ""
                if source.source_type == "document_extraction":
                    document_item_count += 1
                    original_source_texts.append(parent_text)
                if is_document_item:
                    has_document_items = True
                display_items.append({
                    "name": source.original_text,
                    "name_parts": item_name_parts(source),
                    "parent_display_name": metadata.get("parent_display_name", "") if metadata.get("relationship") == "component" else "",
                    "quantity_per_parent": metadata.get("quantity_per_parent") if metadata.get("relationship") == "component" else None,
                    "quantity": source.quantity if source.quantity is not None else notification_item.get("quantity"),
                    "unit": source.unit or notification_item.get("unit", ""),
                    "code": notification_item.get("code", "") if original_source else "",
                    "code_name": notification_item.get("code_name", "") if original_source else "",
                    "price": notification_item.get("price") if quote_source else None,
                    "sum": notification_item.get("sum") if quote_source else None,
                    "characteristics": [row for row in details if row.get("scope") != "completeness"],
                    "completeness": [row for row in details if row.get("scope") == "completeness"],
                    "group_key": origin.pk if origin and metadata.get("relationship") == "component" else None,
                    "supply_kind": metadata.get("supply_kind", ""),
                    "structure_reason": metadata.get("structure_reason", ""),
                    "original_item": notification_by_index.get((origin.metadata or {}).get("notification_index"), {}) if origin else {},
                    "previous_components": [{"name": child.original_text, "quantity": child.quantity, "unit": child.unit,
                                             "facts": effective_item_requirements(child).get("characteristics", [])}
                                            for child in v2_source_items if child.pk in metadata.get("replaces_source_ids", [])],
                    "name_changed": bool(origin and source.original_text != origin.original_text),
                    "name_removed": characteristic_display({"value": source.original_text, "source": "combined", "source_facts": [{"value": origin.original_text, "source": "notification"}]})["removed_text"] if origin else "",
                    "omitted_facts": requirements.get("omitted_facts", []),
                    "source_mark": "Уточнено по документации" if is_item_enrichment(source) and is_document_item else "Из документации" if source.source_type == "document_extraction" and is_document_item else "",
                    "source_mark_kind": "enriched",
                    "source_details": " · ".join(str(part) for part in source_bits if part),
                    "source_evidence": evidence,
                    "original_source_text": parent_text,
                    "source_changes": [detail for detail, raw in zip(details, raw_characteristics) if isinstance(raw, dict) and raw.get("source") == "document"],
                    "composition_question": question.question if question else "",
                    "composition_question_id": question.pk if question else None,
                    "composition_options": list((question.possible_answers or {}).get("options", [])) if question and isinstance(question.possible_answers, dict) else [],
                })
        else:
            notification_sources = {item.metadata.get("notification_index"): item for item in v2_source_items if item.source_type == "notification"}
            for index, item in enumerate(display_items):
                source = notification_sources.get(index)
                question = questions_by_source.get(source.pk) if source else None
                if question:
                    item["composition_question"] = question.question
                    item["composition_question_id"] = question.pk
                    item["composition_options"] = list((question.possible_answers or {}).get("options", [])) if isinstance(question.possible_answers, dict) else []
    display_item_groups, ungrouped_display_items = [], []
    grouped = {}
    for item in display_items:
        if "completeness" not in item:
            item["completeness"] = [row for row in item.get("characteristics", []) if row.get("scope") == "completeness"]
            item["characteristics"] = [row for row in item.get("characteristics", []) if row.get("scope") != "completeness"]
        key = item.get("group_key")
        if not key:
            ungrouped_display_items.append(item)
            continue
        if key not in grouped:
            grouped[key] = {"key": key, "name": item.get("parent_display_name") or item.get("original_item", {}).get("name") or "Состав исходной позиции",
                            "original": item.get("original_item", {}), "items": [], "supply_kind": item.get("supply_kind"), "reason": item.get("structure_reason"), "previous_components": item.get("previous_components", [])}
            display_item_groups.append(grouped[key])
        grouped[key]["items"].append(item)
    for group in display_item_groups:
        group["name_parts"] = characteristic_display({"value": group["name"], "source": "combined", "source_facts": [{"value": group["original"].get("name", ""), "source": "notification"}]})["parts"]
    for group in document_proposal_groups:
        group["can_keep_quote"] = len(group["items"]) == 1 and group["items"][0]["quote_eligible"]
        group["is_kit"] = bool(group["items"]) and all(item["relationship"] == "component" for item in group["items"])
        group["supply_kind"] = group["items"][0].get("supply_kind") if group["items"] else ""
        group["reason"] = group["items"][0].get("structure_reason") if group["items"] else ""
        group["current_items"] = [{"name": source.original_text, "quantity": source.quantity, "unit": source.unit,
                                   "facts": effective_item_requirements(source).get("characteristics", [])}
                                  for source in v2_source_items if source.is_active and source.parent_id == group["parent"].pk] if group["parent"] else []
    v2_job_label = {"queued": "Обрабатывается", "running": "Обрабатывается", "preparing_input": "Собираем данные", "routing": "Определяем маршрут", "needs_review": "Нужна проверка", "ready": "Готово", "partial": "Нужна проверка", "failed": "Нужна проверка"}.get(v2_job.status, "") if v2_job else ""

    # Компактная сводка расчёта прямо на странице тендера (см. концепцию: блок
    # с данными остаётся на каждом пройденном этапе) — цена/прибыль/ROI с учётом
    # прогноза + кнопка в сам расчёт. Тот же verdict_for, что и на странице
    # расчёта — одни и те же цифры, не пересчитываем по-своему.
    calc_verdict = None
    estimate = None
    if estimate_id:
        from tenders.models import TenderEstimate
        from tenders.services import verdict_for

        estimate = TenderEstimate.objects.filter(pk=estimate_id).first()
        if estimate:
            calc_verdict = verdict_for(estimate, tender)

    lifecycle = _tender_lifecycle(tender, estimate)
    active_stage = "review" if estimate is None else (
        "calculation" if tender.outcome_status == Tender.OUTCOME_DRAFT else
        "bidding" if tender.outcome_status == Tender.OUTCOME_PENDING else "result"
    )

    org = Organization.objects.filter(inn=tender.customer_inn).first() if tender.customer_inn else None
    if not analysis_snapshot and tender.law != "fz44" and org is None and tender.customer_inn:
        org = enrich_one_org(tender.customer_inn, tender.law)  # для 223 карточки заказчика больше неоткуда взять

    if is_manual:
        clar_raw, comp_raw = [], []
    elif request.GET.get("workspace"):
        clar_raw, comp_raw = tender.clarifications_raw or [], tender.complaints_raw or []
        force_extras = request.GET.get("refresh") == "1"
        if extras_need_refresh(tender, force=force_extras):
            start_extras_refresh_in_background(tender.pk, force=force_extras)
    else:
        clar_raw, comp_raw = extras_for(tender, force=request.GET.get("refresh") == "1")

    # Прогноз снижения и оценка риска не показываются на «Входящих» — рано,
    # ещё не решили, что тендер вообще стоит смотреть; появляются вместе,
    # начиная с «Проверки» (review != unreviewed).
    stats_diag = {}
    stats = price_stats_for(
        tender, card, diag=stats_diag,
        exclude_purchase_number=tender.purchase_number if tender.outcome_status not in (Tender.OUTCOME_DRAFT, Tender.OUTCOME_PENDING) else None,
    ) if (is_archived or tender.review != Tender.UNREVIEWED) else None
    if stats:
        for row in stats["examples"]:
            row["region_label"] = region_name(row["region"]) if row["region"] else ""

    if is_manual:
        risk_needs_fetch = False
        risk = None
        risk_error = ""
    elif tender.review == Tender.UNREVIEWED and estimate is None:
        # «Входящие» — ещё рано на настоящую (платную) оценку, но бесплатную
        # предварительную сводку по уже разобранному извещению показываем
        # всегда: та же форма таблицы, без документов и без ИИ (см.
        # risk_assessment.preliminary_summary). Кнопки «Оценить риски» тут
        # нет — оценка стартует автоматически по «В работу».
        from .risk_assessment import preliminary_summary

        risk_needs_fetch = False
        risk = preliminary_summary(card) if card else None
        risk_error = ""
    else:
        # Оценка рисков читает документы закупки — может занимать до ~30-40с
        # (сеть до ЕИС). Чтобы это не блокировало открытие карточки, первый
        # расчёт уходит в фон (см. risk_status ниже, дергается JS-ом со
        # спиннером). Уже посчитанное (успех или ошибка — risk_checked_at не
        # пуст) отдаём сразу, без лишнего похода в шлюз.
        risk_needs_fetch = card is not None and (
            tender.risk_checked_at is None or "risk_factors" not in (tender.risk_assessment or {})
        )
        risk = None if risk_needs_fetch else (tender.risk_assessment or None)
        risk_error = "" if risk_needs_fetch else tender.risk_error

    protocol = _protocol_view(tender)
    if analysis_snapshot and resolve_tender_analysis_status(tender).revision != analysis_status.revision:
        return HttpResponse(status=409)
    response = render(request, "tender_selection/_analysis_result.html" if analysis_snapshot else "tender_selection/detail.html", {
        "tender": tender,
        "is_manual": is_manual,
        "display_purchase_number": (tender.raw or {}).get("display_number") or tender.purchase_number,
        "card": card,
        "org": org,
        "region_label": region_name(tender.region) if tender.region else "",
        "fetch_failed": payload is None and tender.law == "fz44",
        "is_fz223": tender.law != "fz44",
        "pushed_estimate_id": estimate_id,
        "pipeline_estimate_id": estimate_id,
        "calc_verdict": calc_verdict,
        "lifecycle": lifecycle,
        "active_stage": active_stage,
        "is_archived": is_archived,
        "archive_stage_label": {key: label for key, label, _ in ARCHIVE_STAGES}.get(_archive_stage(tender, estimate), ""),
        "back_to_archive": is_archived or request.GET.get("from") == "archive",
        "return_url": return_url,
        "forecast_stat": ContractStat.objects.filter(law=tender.law, purchase_number=tender.purchase_number, own_funnel=True).first(),
        "estimate": estimate,
        "v2_source_items": v2_source_items,
        "document_proposals": document_proposals,
        "document_proposal_ids": [item.pk for item in document_proposals],
        "document_proposal_groups": document_proposal_groups,
        "analysis_status": analysis_status,
        "analysis_issues": analysis_issues(v2_job),
        "v2_lines": v2_lines,
        "v2_questions": v2_questions,
        "v2_job": v2_job,
        "v2_job_label": v2_job_label,
        "display_items": display_items,
        "display_item_groups": display_item_groups,
        "ungrouped_display_items": ungrouped_display_items,
        "display_item_count": len(ungrouped_display_items) + len(display_item_groups),
        "source_financials": source_financials,
        "display_source_financials": [row for row in source_financials if row.get("source_item_id") not in grouped],
        "has_document_items": has_document_items,
        "document_item_count": document_item_count,
        "original_source_texts": sorted({text for text in original_source_texts if text}),
        "bid_reduction_percent": reduction_percent_from(tender.max_price, tender.bid_price),
        "forecast_comparison": _market_forecast_comparison(tender),
        "procurement_status": _procurement_status(tender, card) if tender.outcome_status == Tender.OUTCOME_PENDING else None,
        "auction_event": _auction_event(card) if tender.outcome_status == Tender.OUTCOME_PENDING else None,
        "protocol": protocol,
        "rejection_explanation": _rejection_explanation(protocol),
        "show_outcome": bool(
            (estimate and tender.outcome_status != Tender.OUTCOME_DRAFT)
            or (is_archived and (
                tender.protocol or tender.contract_price is not None
                or tender.contract_reduction_percent is not None or tender.outcome_checked_at
            ))
        ),
        "clarifications": parse_clarifications(clar_raw),
        "complaints": parse_complaints(comp_raw),
        "price_stats": stats,
        "price_stats_diag": stats_diag or None,
        "risk_needs_fetch": risk_needs_fetch,
        "risk": risk,
        "risk_error": risk_error,
        "risk_docs": [] if risk_needs_fetch else tender.risk_assessment_docs,
    })

    if analysis_snapshot:
        response["Cache-Control"] = "no-store"
    return response


def _protocol_view(tender):
    """Таблица участников итогового протокола; наша заявка отмечена, если опознана."""
    protocol = tender.protocol or {}
    if not protocol:
        return None
    nmck = protocol.get("nmck") or tender.max_price
    ours = find_ours(protocol, bid_number=tender.bid_number, bid_price=tender.bid_price)
    rows = []
    for participant in sorted(protocol.get("participants", []), key=lambda p: (p.get("rank") is None, p.get("rank") or 0)):
        price = Decimal(participant["price"]) if participant.get("price") else None
        rows.append({
            **participant,
            "price": price,
            "reduction": reduction_percent_from(nmck, price) if price is not None else None,
            "is_ours": participant is ours,
        })
    return {**protocol, "rows": rows, "ours": ours}


def _tender_lifecycle(tender, estimate):
    """Короткий ориентир на карточке: этапы, а не вторая навигация."""
    status = tender.outcome_status if estimate else ""
    current = "incoming" if tender.review == Tender.UNREVIEWED else "evaluation"
    if estimate:
        current = "calculation" if status == "draft" else "bidding" if status == "pending" else "result"

    order = ("incoming", "evaluation", "calculation", "bidding", "result")
    labels = {"incoming": "Входящие", "evaluation": "Оценка", "calculation": "Расчёт", "bidding": "Торги", "result": "Результат"}
    current_index = order.index(current)
    return [
        {"label": labels[key], "state": "done" if index < current_index else "current" if index == current_index else "future"}
        for index, key in enumerate(order)
    ]


@tender_viewer_required
@require_POST
def owner_interaction_answer(request, pk, interaction_pk):
    tender = get_object_or_404(Tender, pk=pk)
    from tenders.models import OwnerFeedbackEvent, OwnerInteraction

    interaction = get_object_or_404(OwnerInteraction, pk=interaction_pk, tender=tender, status="open")
    answer = request.POST.get("answer", "").strip()
    redirect_url = f"{reverse('tender_selection:detail', args=[tender.pk])}#owner-question-{interaction.pk}"
    if not answer:
        messages.error(request, "Введите ответ, чтобы закрыть вопрос.")
        return redirect(redirect_url)
    from .services import answer_tender_owner_question
    try:
        answer_tender_owner_question(request.user, tender.pk, interaction.pk, answer)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_url)
    messages.success(request, "Ответ сохранён для этого тендера.")
    return redirect(redirect_url)


@tender_viewer_required
@require_POST
def document_proposal_action(request, pk, parent_pk):
    tender = get_object_or_404(Tender, pk=pk)
    detail_url = reverse("tender_selection:detail", args=[tender.pk]) + ("?workspace=1" if request.GET.get("workspace") else "")
    decision = request.POST.get("action")
    try:
        decide_document_proposals(request.user, tender.pk, parent_pk, decision)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(detail_url)
    if decision == "accept":
        messages.success(request, "Состав из документации принят. Перечень товаров обновлён.")
    else:
        messages.info(request, "Предложения из документации отклонены.")
    return redirect(detail_url + "#tender-items")


@tender_viewer_required
def risk_status(request, pk):
    """AJAX-эндпоинт для блока «Оценка рисков» на карточке — считает (или берёт из
    кэша) и отдаёт готовый HTML-фрагмент. Чтение документов и запрос к ИИ-шлюзу могут
    занять десятки секунд, поэтому вызывается из JS отдельно от рендера страницы."""
    tender = get_object_or_404(Tender, pk=pk)
    risk = risk_assessment_for(tender, force=request.GET.get("refresh") == "1")
    html = render_to_string("tender_selection/_risk_block.html", {
        "risk": risk, "risk_error": tender.risk_error, "risk_docs": tender.risk_assessment_docs,
    }, request=request)
    return JsonResponse({"html": html})


def _doc_by_idx(tender, idx):
    """(doc-словарь | None, JsonResponse-ошибка | None)."""
    card = parse_notification(tender.notification_raw) if tender.notification_raw else None
    docs = (card or {}).get("documents", [])
    if not 0 <= idx < len(docs):
        return None, JsonResponse({"error": "Документ не найден."}, status=404)
    return docs[idx], None


def _clear_tender_document_previews(tender):
    from .services import clear_tender_document_previews

    return clear_tender_document_previews(tender)




def _result_json(name, result):
    return JsonResponse({"name": name, "kind": result.get("kind", ""),
                         "html": result.get("html", ""), "error": result.get("error", ""),
                         "zip_entries": result.get("zip_entries", [])})


@tender_viewer_required
def doc_preview(request, pk, idx):
    tender = get_object_or_404(Tender, pk=pk)
    if request.GET.get("format") == "pdf":
        from .services import tender_document_pdf
        try:
            content = tender_document_pdf(request.user, tender.pk, idx)
        except (ValueError, DocumentError) as exc:
            return HttpResponse(str(exc), status=422, content_type="text/plain; charset=utf-8")
        response = HttpResponse(content, content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="document.pdf"'
        response["Cache-Control"] = "private, no-store"
        response["X-Frame-Options"] = "SAMEORIGIN"
        return response
    try:
        result = preview_tender_document(request.user, tender.pk, idx, refresh=request.GET.get("refresh") == "1", browse_archive=request.GET.get("browse") == "1")
    except ValueError as exc:
        return _result_json("", {"error": str(exc)})
    return _result_json(result["name"], result)


@tender_viewer_required
@require_POST
def doc_upload(request, pk, idx):
    """Ручной запасной путь: если у сервера вдруг снова не будет сети до ЕИС —
    пользователь скачивает файл сам и загружает сюда, дальше тот же разбор
    (extract_preview), что и при автоскачивании."""
    tender = get_object_or_404(Tender, pk=pk)
    uploaded = request.FILES.get("file")
    try:
        result = upload_tender_document_preview(
            request.user, tender.pk, idx, uploaded.read() if uploaded else b"",
        )
    except ValueError as exc:
        return _result_json("", {"error": str(exc)})
    return _result_json(result["name"], result)


@tender_viewer_required
def doc_zip_entry(request, pk, idx, entry):
    """Провал внутрь многофайлового архива: качаем документ заново (файл-то один
    и тот же — кэш предпросмотра держит только итог разбора КОНКРЕТНОГО вложенного
    файла, не сырые байты архива) и достаём из него нужный вложенный файл."""
    tender = get_object_or_404(Tender, pk=pk)
    doc, err = _doc_by_idx(tender, idx)
    if err:
        return err
    url, name = doc.get("url", ""), doc.get("name", "")

    try:
        data = _fetch_doc_bytes(tender, url, name)
    except DocumentError as exc:
        return _result_json(entry, {"error": str(exc)})

    try:
        parents = request.GET.getlist("parent")
        if len(parents) > 5:
            raise DocumentError("Слишком много вложенных архивов.")
        for path in parents + [entry]:
            data = extract_zip_entry(data, path)
            if data is None:
                raise DocumentError("Файл не найден в архиве.")
        if request.GET.get("format") == "pdf":
            from .office import convert_office
            content = data if data.startswith(b"%PDF") else convert_office(data, entry, "pdf")
            response = HttpResponse(content, content_type="application/pdf")
            response["Content-Disposition"] = 'inline; filename="document.pdf"'
            response["Cache-Control"] = "private, no-store"
            response["X-Frame-Options"] = "SAMEORIGIN"
            return response
        return _result_json(entry, extract_preview(data, entry, browse_archive=True))
    except DocumentError as exc:
        if request.GET.get("format") == "pdf":
            return HttpResponse(str(exc), status=422, content_type="text/plain; charset=utf-8")
        return _result_json(entry, {"error": str(exc)})


@superuser_required
def eis_diag(request):
    """Разовая диагностика: реально ли прод-сервер видит сеть ЕИС на уровне TCP/HTTPS,
    или заблокирован весь домен zakupki.gov.ru (а не только конкретная ссылка/метод).
    Ничего не сохраняет, только сетевые зонды с прод-машины."""
    import socket
    import time
    from urllib.error import HTTPError, URLError
    from urllib.request import Request as _Req
    from urllib.request import urlopen as _urlopen

    results = []

    def probe_tcp(label, host, port=443, timeout=8):
        t0 = time.monotonic()
        try:
            conn = socket.create_connection((host, port), timeout=timeout)
            conn.close()
            results.append({"probe": label, "ok": True, "ms": round((time.monotonic() - t0) * 1000)})
        except Exception as exc:
            results.append({"probe": label, "ok": False, "ms": round((time.monotonic() - t0) * 1000),
                             "error": f"{type(exc).__name__}: {exc}"})

    def probe_http(label, url, timeout=10):
        t0 = time.monotonic()
        try:
            req = _Req(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with _urlopen(req, timeout=timeout) as resp:
                results.append({"probe": label, "ok": True, "ms": round((time.monotonic() - t0) * 1000),
                                 "status": resp.status})
        except HTTPError as exc:
            # HTTP-ошибка — значит соединение и TLS прошли, портал ответил (это НЕ таймаут)
            results.append({"probe": label, "ok": True, "ms": round((time.monotonic() - t0) * 1000),
                             "status": exc.code, "note": "сервер ответил (пусть и ошибкой) — сеть не блокирует"})
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            results.append({"probe": label, "ok": False, "ms": round((time.monotonic() - t0) * 1000),
                             "error": f"{type(exc).__name__}: {exc}"})

    # свой внешний IP — чтобы проверить снаружи (по базам geo/ASN), где он реально числится,
    # независимо от того, что написано в личном кабинете Timeweb
    my_ip = None
    try:
        req = _Req("https://api.ipify.org?format=json")
        with _urlopen(req, timeout=8) as resp:
            import json as _json
            my_ip = _json.loads(resp.read().decode("utf-8")).get("ip")
        results.append({"probe": "свой внешний IP", "ok": True, "ms": 0, "note": my_ip})
    except Exception as exc:
        results.append({"probe": "свой внешний IP", "ok": False, "ms": 0, "error": f"{type(exc).__name__}: {exc}"})

    # контроль: то, что точно работает (автосбор дёргает это же каждые 30 мин)
    probe_tcp("TCP v2test.gosplan.info (контроль, точно работает)", "v2test.gosplan.info")
    probe_tcp("TCP zakupki.gov.ru", "zakupki.gov.ru")
    probe_tcp("TCP int44.zakupki.gov.ru", "int44.zakupki.gov.ru")
    probe_http("GET https://zakupki.gov.ru/ (главная, не файл)", "https://zakupki.gov.ru/")

    # масштаб блокировки: только ЕИС или весь рунет с этого сервера?
    probe_tcp("TCP www.gosuslugi.ru (другой gov.ru)", "www.gosuslugi.ru")
    probe_tcp("TCP www.nalog.gov.ru (другой gov.ru)", "www.nalog.gov.ru")
    probe_tcp("TCP www.cbr.ru (ЦБ РФ, не gov.ru)", "www.cbr.ru")
    probe_tcp("TCP ya.ru (обычный рунет, контроль)", "ya.ru")

    return JsonResponse({"results": results, "my_ip": my_ip})


@superuser_required
def filter_settings(request):
    settings = FilterSettings.load()
    if request.method == "POST":
        from .services import save_selection_settings

        try:
            save_selection_settings({
                "include_words": request.POST.get("include_words", ""),
                "exclude_words": request.POST.get("exclude_words", ""),
                "profile_triage_enabled": request.POST.get("profile_triage_enabled") == "1",
                "min_price": request.POST.get("min_price") or 0,
                "window_days": request.POST.get("window_days") or 7,
                "okpd2_codes": request.POST.getlist("okpd2"),
                "regions": [r for r in request.POST.getlist("region") if r.isdigit()],
                "laws": [law for law in request.POST.getlist("law") if law in ("fz44", "fz223")] or ["fz44"],
            })
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("tender_selection:settings")
        messages.success(request, "Настройки подбора сохранены.")
        return redirect("tender_selection:list")

    chosen_codes = set(effective_okpd2(settings))
    chosen_regions = set(str(r) for r in (settings.regions or []))
    chosen_laws = set(effective_laws(settings))
    return render(request, "tender_selection/settings.html", {
        "settings": settings,
        "categories": [(code, label, code in chosen_codes) for code, label in CATEGORY_GROUPS],
        "regions": [(code, name, str(code) in chosen_regions) for code, name in sorted(REGION_NAMES.items(), key=lambda x: x[1])],
        "laws": [(code, label, code in chosen_laws) for code, label in Tender.LAW_CHOICES],
        "using_defaults": not settings.okpd2_codes,
    })


@superuser_required
def evaluation_settings(request):
    """Все числа, по которым оцениваются тендеры: пороги сроков и уровни факторов
    риска, светофор ROI (из него же целевая и минимальная цена на торгах), НДС и
    снижение по умолчанию для расчёта, прогноз снижения, срок хранения «Входящих»."""
    from tenders.models import TenderSettings

    from .risk_policy import DEFAULT_FACTOR_LEVELS

    filters = FilterSettings.load()
    tender_settings = TenderSettings.objects.get_or_create(pk=1)[0]
    form = {
        name: getattr(tender_settings if name in _TENDER_SETTINGS_FIELDS else filters, name)
        for name, *_ in EVALUATION_NUMBERS
    }
    levels = {**DEFAULT_FACTOR_LEVELS, **(filters.risk_factor_levels or {})}
    if request.method == "POST":
        values, errors = evaluation_settings_form(request.POST)
        if not errors:
            save_evaluation_settings(request.POST)
            messages.success(request, "Настройки оценки сохранены.")
            return redirect("tender_selection:evaluation_settings")
        for error in errors:
            messages.error(request, error)
        form = {name: request.POST.get(name, "") for name in form}
        levels = values["risk_factor_levels"]
    return render(request, "tender_selection/evaluation_settings.html", {
        "form": form,
        "factor_levels": [(key, label, levels.get(key, "")) for key, label in RISK_FACTOR_LABELS],
        "level_choices": RISK_LEVEL_CHOICES,
    })


@superuser_required
def word_audit_page(request):
    """Статистика плюс/минус-слов (без ИИ) и последний AI-аудит с галочками для применения."""
    from . import word_audit
    from .models import WordAudit

    return render(request, "tender_selection/word_audit.html", {
        "stats": word_audit.word_stats(),
        "audit": WordAudit.objects.first(),
        "settings": FilterSettings.load(),
    })


@superuser_required
@require_POST
def word_audit_run(request):
    from .ai_gateway import AIGatewayError

    try:
        audit = run_filter_word_audit(request.user)
    except AIGatewayError as exc:
        messages.error(request, f"Аудит не выполнен: {exc}")
    else:
        spent = f" Потрачено {audit['spend_rub']} ₽." if audit["spend_rub"] is not None else ""
        messages.success(request, f"Аудит готов.{spent}")
    return redirect("tender_selection:word_audit")


@superuser_required
@require_POST
def word_audit_apply(request):
    from . import word_audit

    chosen = {kind: [w for w in request.POST.getlist(kind) if w.strip()] for kind in word_audit.SUGGESTION_KINDS}
    try:
        apply_filter_word_audit(request.user, chosen)
    except ValueError:
        messages.info(request, "Ничего не выбрано.")
        return redirect("tender_selection:word_audit")
    messages.success(request, "Фильтр обновлён: " + ", ".join(
        f"{label} {len(chosen[kind])}" for kind, label in (
            ("add_plus", "+плюс"), ("add_minus", "+минус"), ("remove_plus", "−плюс"), ("remove_minus", "−минус"),
        ) if chosen[kind]
    ) + ".")
    return redirect("tender_selection:word_audit")


@superuser_required
@require_POST
def profile_triage_run(request):
    try:
        result = run_profile_triage(request.user)
    except ValueError as exc:
        messages.info(request, str(exc) + ".")
        return redirect("tender_selection:list")
    messages.success(request, f"Jev начал проверку {result['started']} входящих. Отметки появятся после обновления страницы.")
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")


@superuser_required
@require_POST
def pull_now(request):
    run = pull_tenders_now(request.user)
    if run["ok"]:
        messages.success(
            request,
            f"Выгрузка: запросов {run['requests_made']}, записей {run['records_received']}, "
            f"новых {run['created_count']}, за {run['duration_seconds']} сек.",
        )
    else:
        messages.error(request, f"Выгрузка не удалась: {run['error']}")
    return redirect("tender_selection:list")


@superuser_required
@require_POST
def push_estimate(request, pk):
    tender = get_object_or_404(Tender, pk=pk)
    if tender.status == Tender.PUSHED and tender.estimates.exists():
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
        return redirect("tender_selection:detail", pk=tender.pk)
    try:
        create_tender_estimate(request.user, tender.pk)
    except ValueError as exc:
        messages.error(request, f"Не удалось создать просчёт: {exc}")
        return redirect("tender_selection:detail", pk=pk)
    messages.success(request, "Просчёт создан. Позиции подставлены из извещения.")
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
    return redirect("tender_selection:detail", pk=tender.pk)


@superuser_required
@require_POST
def enter_outcome(request, pk):
    """Обязательный шаг канбана для карточек «Торги» — забрать факт торгов
    (автоматически через ГосПлан) или подтвердить его вручную, если по номеру
    закупки контракт ещё не найден или свой ИНН не настроен (COMPANY_INN)."""
    from tenders.models import TenderEstimate

    try:
        result = record_tender_outcome(
            request.user, pk, status=request.POST.get("status"),
            actual_reduction_percent=request.POST.get("actual_reduction_percent", ""),
        )
    except ValueError as exc:
        messages.error(request, str(exc) + ".")
        return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")
    if result.get("warning"):
        messages.warning(request, result["warning"] + ".")
    elif result["lifecycle_changed"]:
        messages.success(request, "Итог торгов обновлён.")
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        if result["lifecycle_changed"]:
            return JsonResponse({"lifecycle_changed": True, "tender_id": result["tender_id"]})
        return JsonResponse({"lifecycle_changed": False, "refresh_detail": True})
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")


@superuser_required
@require_POST
def save_bid(request, pk):
    """Номер и/или сумма нашей заявки — по ним находим себя в протоколе."""
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    try:
        save_tender_bid(request.user, estimate.pk, bid_number=request.POST.get("bid_number", ""), bid_price=request.POST.get("bid_price"))
    except ValueError as exc:
        messages.error(request, str(exc) + ".")
        return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")
    messages.success(request, "Данные нашей заявки сохранены.")
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")


@superuser_required
@require_POST
def dismiss(request, pk):
    tender = get_object_or_404(Tender, pk=pk)
    hide_tender(request.user, tender.pk, reason=request.POST.get("reason"))
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
    return _dismiss_redirect(request)


@superuser_required
@require_POST
def dismiss_estimate(request, pk):
    """Архивировать карточку Tender со стадии расчёта, торгов или результата."""
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    tender = get_object_or_404(Tender, pk=estimate.tender_id)
    hide_tender(request.user, tender.pk)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
    return _dismiss_redirect(request)


@superuser_required
@require_POST
def restore(request, pk):
    tender = get_object_or_404(Tender, pk=pk, status=Tender.DISMISSED)
    restore_hidden_tender(request.user, tender.pk)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
    return redirect(f"{reverse('tender_selection:list')}?view=kanban")


@superuser_required
@require_POST
def restore_estimate(request, pk):
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    return restore(request, estimate.tender_id)


@superuser_required
@require_POST
def toggle_forecast(request, pk):
    tender = get_object_or_404(Tender, pk=pk, status=Tender.DISMISSED)
    try:
        result = change_forecast_inclusion(request.user, tender.pk)
    except ValueError as exc:
        messages.warning(request, str(exc) + ".")
    else:
        messages.success(request, "Результат добавлен в прогноз." if result["forecast_included"] else "Результат исключён из прогноза.")
    return redirect(f"{reverse('tender_selection:detail', args=[pk])}?from=archive")


# Причину скрытия не спрашиваем: её задаёт стадия, на которой тендер ушёл в архив.
ARCHIVE_STAGES = (
    ("incoming", "Входящие", "не наш профиль"),
    ("evaluation", "Оценка", "риски"),
    ("calculation", "Расчёт", "нерентабельно"),
    ("bidding", "Торги", "без итога"),
    ("published", "Итог без нас", "наша заявка не отмечена"),
    ("lost", "Проиграли", ""),
    # Пока нет «Исполнения», выигранные тоже уходят сюда.
    ("won", "Выиграли", ""),
)
ARCHIVE_SORTS = {
    "archived": F("archived_at").desc(nulls_last=True),
    "price_hi": SORTS["price_hi"],
    "price_lo": SORTS["price_lo"],
    "published": SORTS["new"],
}


def _archive_stage(tender, estimate) -> str:
    if tender.archived_from_stage:
        return tender.archived_from_stage
    return archive_stage_for(tender)


@superuser_required
def archive(request):
    """Архив скрытых тендеров: поиск, фильтр по стадии скрытия (она же причина),
    закону и итогу торгов, с возможностью восстановления."""
    from tenders.models import TenderEstimate

    query = request.GET.get("q", "").strip()
    stage_filter = request.GET.get("stage") if request.GET.get("stage") in dict((k, 1) for k, *_ in ARCHIVE_STAGES) else "all"
    law_filter = request.GET.get("law") if request.GET.get("law") in ("fz44", "fz223") else "all"
    sort = request.GET.get("sort") if request.GET.get("sort") in ARCHIVE_SORTS else "archived"

    selection_settings = FilterSettings.load()
    tenders = Tender.objects.filter(status=Tender.DISMISSED)
    if law_filter != "all":
        tenders = tenders.filter(law=law_filter)
    tenders = list(tenders.order_by(ARCHIVE_SORTS[sort], "-pk"))
    orgs = {o.inn: o for o in Organization.objects.filter(inn__in={t.customer_inn for t in tenders if t.customer_inn})}
    latest_estimate = {}
    for estimate in TenderEstimate.objects.filter(tender__in=tenders).order_by("updated_at"):
        latest_estimate[estimate.tender_id] = estimate

    needle = query.casefold()
    rows = []
    for tender in tenders:
        tender.org = orgs.get(tender.customer_inn)
        haystack = " ".join([
            tender.title, tender.object_info, tender.purchase_number, tender.customer_inn,
            tender.org.name if tender.org else "",
        ]).casefold()
        if needle and needle not in haystack:
            continue
        estimate = latest_estimate.get(tender.pk)
        rows.append({"tender": tender, "estimate": estimate, "stage": _archive_stage(tender, estimate)})

    stage_counts = {key: 0 for key, *_ in ARCHIVE_STAGES}
    for row in rows:
        stage_counts[row["stage"]] += 1
    stages = [{"key": "all", "label": "Все", "hint": "", "count": len(rows)}] + [
        {"key": key, "label": label, "hint": hint, "count": stage_counts[key]} for key, label, hint in ARCHIVE_STAGES
    ]
    if stage_filter != "all":
        rows = [row for row in rows if row["stage"] == stage_filter]

    page = Paginator(rows, 100).get_page(request.GET.get("page"))
    stage_labels = {key: label for key, label, _ in ARCHIVE_STAGES}
    for row in page.object_list:
        tender, estimate = row["tender"], row["estimate"]
        tender.region_label = region_name(tender.region) if tender.region else ""
        tender.law_label = LAW_LABELS.get(tender.law, tender.law)
        row["stage_label"] = stage_labels[row["stage"]]
        protocol = tender.protocol or {}
        row["ours"] = find_ours(protocol, bid_number=tender.bid_number, bid_price=tender.bid_price) if protocol else None
        row["participants"] = len(protocol.get("participants", []))

    return render(request, "tender_selection/archive.html", {
        "page_obj": page,
        "stages": stages,
        "stage_filter": stage_filter,
        "law_filter": law_filter,
        "sort": sort,
        "query": query,
        "nav_counts": {"incoming": _incoming_count(selection_settings), "board": _board_count(selection_settings)},
    })


@superuser_required
@require_POST
def set_review(request, pk):
    tender = get_object_or_404(Tender, pk=pk)
    value = request.POST.get("review", "")
    try:
        update_tender_review(request.user, tender.pk, value)
    except ValueError:
        pass
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")


@tender_viewer_required
@require_POST
def refresh_analysis(request, pk):
    from .services import refresh_document_analysis
    tender = get_object_or_404(Tender, pk=pk)
    try:
        result = refresh_document_analysis(request.user, tender.pk)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"tender_id": tender.pk, "label": "Проверяем ТЗ…", **result})
        messages.info(request, result["message"])
    except ValueError as exc:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"error": str(exc)}, status=400)
        messages.error(request, str(exc))
    target = request.POST.get("next", "")
    if not target or not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = reverse("tender_selection:detail", args=[pk]) + ("?workspace=1" if request.GET.get("workspace") else "")
    return redirect(target)


@tender_viewer_required
@require_POST
def all_document_proposals_action(request, pk):
    from .services import decide_all_document_proposals
    get_object_or_404(Tender, pk=pk)
    try:
        ids = [int(value) for value in request.POST.get("proposal_ids", "").split(",") if value]
        decide_all_document_proposals(request.user, pk, request.POST.get("action"), ids)
        messages.success(request, "Все правки приняты." if request.POST.get("action") == "accept" else "Все правки отклонены.")
    except ValueError as exc:
        messages.error(request, str(exc))
    target = reverse("tender_selection:detail", args=[pk]) + ("?workspace=1" if request.GET.get("workspace") else "")
    return redirect(target + "#tender-items")


@superuser_required
@require_POST
def import_by_number(request):
    from .services import import_tender_by_number
    as_json = request.headers.get('Accept') == 'application/json'
    try:
        result = import_tender_by_number(request.user, request.POST.get('purchase_number'))
    except ValueError as exc:
        if as_json:
            return JsonResponse({'error': str(exc)}, status=400)
        messages.error(request, str(exc))
        return redirect('tender_selection:list')
    url = reverse('tender_selection:detail', args=[result['tender_id']])
    if as_json:
        return JsonResponse({**result, 'url': url})
    return redirect(url)


@login_required
@require_GET
@never_cache
def analysis_status(request):
    from tenders.analysis_presentation import resolve_tender_analysis_statuses
    values = request.GET.get('ids', '').split(',')
    if not values or len(values) > 200 or any(not value.isascii() or not value.isdigit() for value in values):
        return JsonResponse({'error': 'Нужен список тендеров'}, status=400)
    if any(len(value) > 19 or not 0 < int(value) <= 9223372036854775807 for value in values):
        return JsonResponse({'error': 'Некорректный номер записи'}, status=400)
    ids = list(dict.fromkeys(int(value) for value in values))
    tenders = list(Tender.objects.filter(pk__in=ids))
    if any(not _tender_viewable_by(request.user, tender) for tender in tenders):
        raise PermissionDenied
    statuses = resolve_tender_analysis_statuses(tenders)
    return JsonResponse({'statuses': [{
        'tender_id': tender.pk, 'code': status.code, 'label': status.label, 'kind': status.kind,
        'reason': status.reason, 'revision': status.revision, 'pending_count': status.pending_count,
        'processing': status.processing, 'job_id': status.job_id, 'issues': status.issues,
        'result_url': reverse('tender_selection:detail', args=[tender.pk]) + '?analysis_snapshot=1',
    } for tender in tenders for status in [statuses[tender.pk]]]})
