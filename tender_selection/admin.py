from django.contrib import admin
from django.core.exceptions import PermissionDenied

from .models import (
    ContractStat, DocumentPreview, FilterSettings, Organization, PullRun, StatsRun, Tender, TenderHistory,
)


@admin.register(DocumentPreview)
class DocumentPreviewAdmin(admin.ModelAdmin):
    list_display = ("filename", "kind", "error", "fetched_at")
    search_fields = ("filename", "url")
    readonly_fields = ("html",)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("inn", "name", "city", "region_name", "checked_at")
    search_fields = ("inn", "name", "city")


@admin.register(FilterSettings)
class FilterSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not FilterSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Tender)
class TenderAdmin(admin.ModelAdmin):
    list_display = ("purchase_number", "law", "title", "max_price", "region", "review", "published_at", "status")
    list_filter = ("law", "review", "status", "stage", "purchase_type")
    list_editable = ("review",)
    search_fields = ("purchase_number", "title", "object_info", "customer_inn")
    readonly_fields = (
        "raw", "notification_raw", "clarifications_raw", "complaints_raw",
        "risk_assessment", "risk_assessment_docs", "first_seen_at",
    )
    date_hierarchy = "published_at"


@admin.register(PullRun)
class PullRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "ok", "requests_made", "records_received", "created_count", "updated_count", "duration_seconds")
    list_filter = ("ok",)
    readonly_fields = tuple(f.name for f in PullRun._meta.fields)

    def has_add_permission(self, request):
        return False


@admin.register(ContractStat)
class ContractStatAdmin(admin.ModelAdmin):
    list_display = ("purchase_number", "category", "region", "nmck", "final_price", "discount_pct",
                    "shared_purchase", "contract_date", "nmck_checked")
    list_filter = ("category", "nmck_checked", "shared_purchase", "is_ours")
    search_fields = ("purchase_number", "contract_reg_num", "subject", "winner_inn")
    date_hierarchy = "contract_date"


@admin.register(StatsRun)
class StatsRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "ok", "requests_made", "contracts_seen", "created_count", "filled_count", "duration_seconds")
    list_filter = ("ok",)
    readonly_fields = tuple(f.name for f in StatsRun._meta.fields)

    def has_add_permission(self, request):
        return False


@admin.register(TenderHistory)
class TenderHistoryAdmin(admin.ModelAdmin):
    list_display = ("purchase_number", "source", "title", "max_price", "archived_from_stage",
                    "reason_code", "decision_source", "outcome_status", "updated_at")
    list_filter = ("source", "law", "status", "archived_from_stage", "reason_code", "decision_source", "outcome_status")
    search_fields = ("purchase_number", "title", "customer_inn", "winner_inn", "reason_text")
    date_hierarchy = "purged_at"
    list_select_related = ("tender",)
    readonly_fields = tuple(field.name for field in TenderHistory._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (obj is None or obj.tender_id is None)

    def delete_queryset(self, request, queryset):
        if queryset.filter(tender__isnull=False).exists():
            raise PermissionDenied("История связана с рабочей карточкой. Сначала завершите работу с ней; расчёты здесь не удаляются.")
        super().delete_queryset(request, queryset)
