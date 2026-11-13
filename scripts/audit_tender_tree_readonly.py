"""TEST-only selective read-only verification of the deployed tender working tree."""
import json
import time
import resource
from collections import Counter
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Count
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from tender_selection.models import Tender
from tenders.calculation_v2_pipeline import active_calculation_items
from tenders.debug_tree import tender_tree_debug
from tenders.models import (
    CalculationComponent, ComponentRoutePlan, TenderCommercialItem,
    TenderComputeLine, TenderComputeWorkUnit, TenderSourceItem,
)
from tenders.working_items import tender_working_items


def select_guard(execute, sql, params, many, context):
    op = sql.lstrip().split(None, 1)[0].upper()
    if op not in ("SELECT", "WITH", "SHOW"):
        raise AssertionError("Non-read-only SQL: " + op)
    return execute(sql, params, many, context)


def components_of(item):
    def descend(nodes):
        for node in nodes:
            yield node
            yield from descend(node.get("components", []))
    yield from descend(item.get("components", []))
    yield from descend(item.get("historical_components", []))


def refs_in(items):
    refs = []
    def records(owner):
        for ref_group in owner.get("calculations", {}).values():
            refs.extend(ref_group)
    for item in items:
        records(item)
        for comp in components_of(item):
            records(comp)
            for route in comp["routes"]:
                for step in route["steps"]:
                    records(step)
        for route in item.get("shared_routes", []):
            for step in route["steps"]:
                records(step)
    return refs


def route_ids(item):
    plans = {route["id"] for route in item.get("shared_routes", [])}
    for comp in components_of(item):
        plans.update(route["id"] for route in comp["routes"])
    return plans


def inspect_tender(label, tender):
    before = time.monotonic()
    with CaptureQueriesContext(connection) as queries, connection.execute_wrapper(select_guard):
        tree = tender_working_items(tender)
    duration = round(time.monotonic() - before, 3)
    items = tree["items"]
    active_sources = {s.pk for s in active_calculation_items(tender)}
    shown_sources = {s["id"] for item in items for s in item["sources"]}
    commercial_ids = {item["id"] for item in items if item["kind"] == "working"}
    expected_commercial_ids = set(TenderCommercialItem.objects.filter(tender=tender, status="active").values_list("pk", flat=True))
    shown_component_ids = [c["id"] for item in items for c in components_of(item)]
    expected_component_ids = set(CalculationComponent.objects.filter(commercial_item_id__in=expected_commercial_ids).values_list("pk", flat=True))
    expected_plan_ids = set(ComponentRoutePlan.objects.filter(commercial_item_id__in=expected_commercial_ids).values_list("pk", flat=True))
    shown_plan_ids = {plan_id for item in items for plan_id in route_ids(item)}
    expected_line_count = TenderComputeLine.objects.filter(job__tender=tender).count()
    expected_unit_count = TenderComputeWorkUnit.objects.filter(job__tender=tender).count()
    registry = tree["calculation_results"]
    all_refs = refs_in(items)
    unattached = tree["unattached_calculations"]
    errors = []
    if not active_sources.issubset(shown_sources):
        errors.append("active_source_missing=" + str(len(active_sources - shown_sources)))
    if expected_commercial_ids != commercial_ids:
        errors.append("commercial_item_mismatch")
    if expected_component_ids != set(shown_component_ids) or len(shown_component_ids) != len(set(shown_component_ids)):
        errors.append("component_loss_or_duplicate")
    if expected_plan_ids != shown_plan_ids:
        errors.append("route_plan_missing=" + str(len(expected_plan_ids - shown_plan_ids)))
    if len(registry) != expected_line_count + expected_unit_count:
        errors.append("result_registry_loss")
    if set(all_refs) - set(registry):
        errors.append("dangling_result_reference")
    if set(unattached) - set(registry):
        errors.append("unattached_missing_registry")
    if any(item.get("state") == "ready" and item.get("issues") for item in items):
        errors.append("ready_despite_reported_issues")
    notes = {
        "nested_quantity_unknown": sum(c["quantity"] is None for item in items for c in components_of(item)),
        "explicit_issues": sum(len(item.get("issues", [])) + sum(len(c.get("issues", [])) for c in components_of(item)) for item in items),
        "unverified_results": sum(r["freshness"] == "unknown" for r in registry.values()),
        "historical_results": sum(r["historical"] for r in registry.values()),
        "source_only_items": sum(item["kind"] == "source" for item in items),
        "working_items": len(commercial_ids),
        "set_items": sum(item.get("structure") == "composite" for item in items),
        "shared_routes": sum(len(item.get("shared_routes", [])) for item in items),
        "notice_price_groups": sum(bool(item.get("notice_prices")) for item in items),
    }
    print(json.dumps({
        "label": label, "tender_pk": tender.pk, "purchase_number": tender.purchase_number,
        "sources": len(active_sources), "items": len(items), "components": len(shown_component_ids),
        "route_plans": len(shown_plan_ids), "line_results": expected_line_count,
        "work_unit_results": expected_unit_count, "result_refs": len(all_refs),
        "unattached": len(unattached), "queries": len(queries), "seconds": duration,
        "notes": notes, "errors": errors,
    }, ensure_ascii=False), flush=True)
    return errors


def pick_cases():
    # Start with small, bounded real tenders; large tender is tested separately.
    chosen, seen = [], set()
    def pick(label, pk):
        if pk is None or pk in seen:
            return
        tender = Tender.objects.filter(pk=pk).first()
        if tender:
            chosen.append((label, tender))
            seen.add(pk)
    small_tenders = list(
        TenderSourceItem.objects.filter(is_active=True)
        .values("tender_id").annotate(n=Count("pk"))
        .filter(n__gte=1, n__lte=8).order_by("-tender_id")[:60]
    )
    small = {r["tender_id"] for r in small_tenders}
    for pk in (TenderCommercialItem.objects.filter(tender_id__in=small, status="active", structure="composite").order_by("-pk").values_list("tender_id", flat=True)[:10]):
        pick("small_composite", pk)
        break
    for pk in ComponentRoutePlan.objects.filter(commercial_item__tender_id__in=small, scope="shared").values_list("commercial_item__tender_id", flat=True)[:10]:
        pick("small_shared", pk)
        break
    for pk in TenderComputeLine.objects.filter(job__tender_id__in=small).exclude(result={}).values_list("job__tender_id", flat=True)[:10]:
        pick("small_result", pk)
        break
    for pk in small:
        pick("small_other", pk)
        if len(chosen) >= 4:
            break
    return chosen[:4]


def main():
    # Cap this process below the container memory to avoid an OOM kill.
    resource.setrlimit(resource.RLIMIT_AS, (450 * 1024 * 1024, 450 * 1024 * 1024))
    if connection.vendor != "postgresql":
        raise RuntimeError("This audit is strictly for TEST PostgreSQL.")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
        stats = {
            "commercial_active": TenderCommercialItem.objects.filter(status="active").count(),
            "commercial_composite": TenderCommercialItem.objects.filter(status="active", structure="composite").count(),
            "components_active": CalculationComponent.objects.filter(status="active").count(),
            "shared_plans": ComponentRoutePlan.objects.filter(scope="shared").count(),
            "operation_plans": ComponentRoutePlan.objects.filter(scope="component").count(),
            "work_units": TenderComputeWorkUnit.objects.count(),
        }
        print(json.dumps({"TEST_GRAPH_COUNTS":stats}), flush=True)
        cases = pick_cases()
        errors = []
        print("READ_ONLY_TEST_AUDIT: " + str(len(cases)) + " selected cases", flush=True)
        if not cases:
            raise RuntimeError("TEST has no tender records.")
        for label, tender in cases:
            try:
                errors.extend(inspect_tender(label, tender))
            except Exception as exc:
                msg = type(exc).__name__ + ": " + str(exc)[:300]
                errors.append("tender " + str(tender.pk) + " exception " + msg)
                print(json.dumps({"label":label,"tender_pk":tender.pk,"exception":msg},ensure_ascii=False),flush=True)
        staff = get_user_model().objects.filter(is_staff=True, is_active=True).first()
        if staff:
            try:
                tender = cases[0][1]
                request = RequestFactory().get("/debug/tender-tree/%s/" % tender.pk)
                request.user = staff
                with connection.execute_wrapper(select_guard):
                    response = tender_tree_debug(request, pk=tender.pk)
                html = response.content.decode("utf-8")
                page_ok = response.status_code == 200 and "Дерево тендера" in html
                print(json.dumps({"html_check":{"status_code": response.status_code, "render_ok":page_ok}},ensure_ascii=False),flush=True)
                if not page_ok:
                    errors.append("debug_html_invalid")
            except Exception as exc:
                msg = type(exc).__name__ + ": " + str(exc)[:300]
                print(json.dumps({"html_check":{"exception":msg}},ensure_ascii=False),flush=True)
                errors.append("debug_html_exception")
        else:
            print(json.dumps({"html_check":"skipped_no_active_staff_user"}),flush=True)
        print(json.dumps({"AUDIT_RESULT":"PASS" if not errors else "FAIL", "case_count":len(cases),"errors":errors},ensure_ascii=False),flush=True)
        if errors:
            raise SystemExit(1)


main()
