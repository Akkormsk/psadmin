from django.contrib import admin

from .models import CatalogCategory, CatalogMatchDecision, CatalogProduct, CatalogSupplier, CatalogSyncRun, ProcessDefinition, ProductionTrainingExample, ProductionTrainingSession, ProductionTrainingTurn, ProductionType, TenderEstimate, TenderKnowledgeSource, TenderLine, TenderSettings


class TenderLineInline(admin.TabularInline):
    model = TenderLine
    extra = 0


@admin.register(TenderEstimate)
class TenderEstimateAdmin(admin.ModelAdmin):
    list_display = ("tender_number", "name", "owner", "reduction_percent", "updated_at")
    list_filter = ("owner", "updated_at")
    search_fields = ("tender_number", "name", "owner__first_name", "owner__last_name")
    autocomplete_fields = ("owner",)
    inlines = (TenderLineInline,)


@admin.register(TenderSettings)
class TenderSettingsAdmin(admin.ModelAdmin):
    list_display = ("vat_rate",)

    def has_add_permission(self, request):
        return not TenderSettings.objects.exists()


@admin.register(ProductionType)
class ProductionTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")


@admin.register(ProductionTrainingExample)
class ProductionTrainingExampleAdmin(admin.ModelAdmin):
    list_display = ("position_name", "production_type", "created_by", "created_at")
    list_filter = ("production_type", "created_by")
    search_fields = ("position_name", "note")


@admin.register(ProcessDefinition)
class ProcessDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "role", "is_active")
    list_filter = ("role", "is_active")
    list_editable = ("is_active",)
    search_fields = ("name", "description")


class ProductionTrainingTurnInline(admin.TabularInline):
    model = ProductionTrainingTurn
    extra = 0
    fields = ("feedback", "understood_changes", "created_at")
    readonly_fields = fields


@admin.register(ProductionTrainingSession)
class ProductionTrainingSessionAdmin(admin.ModelAdmin):
    list_display = ("position_name", "created_by", "is_confirmed", "updated_at")
    list_filter = ("is_confirmed", "created_by")
    search_fields = ("position_name",)
    readonly_fields = ("requirements", "current_hypothesis", "confirmed_example", "created_at", "updated_at")
    inlines = (ProductionTrainingTurnInline,)


@admin.register(TenderKnowledgeSource)
class TenderKnowledgeSourceAdmin(admin.ModelAdmin):
    list_display = ("title", "supplier_name", "source_type", "updated_at", "is_active")
    list_filter = ("source_type", "is_active", "created_by")
    search_fields = ("title", "supplier_name", "url", "content_summary")
    list_editable = ("is_active",)


@admin.register(CatalogSupplier)
class CatalogSupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "sync_status", "last_synced_at", "is_active")
    list_filter = ("sync_status", "is_active")
    readonly_fields = ("last_synced_at", "sync_status", "sync_message")


@admin.register(CatalogCategory)
class CatalogCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "path", "supplier", "is_active")
    list_filter = ("supplier", "is_active")
    search_fields = ("name", "path", "external_id")


@admin.register(CatalogProduct)
class CatalogProductAdmin(admin.ModelAdmin):
    list_display = ("article", "name", "brand", "effective_price", "total_stock", "supplier", "is_active")
    list_filter = ("supplier", "is_active", "is_on_order", "brand")
    search_fields = ("article", "article_base", "name", "full_name", "search_text")
    readonly_fields = ("synced_at", "source_updated_at", "raw_data")


@admin.register(CatalogSyncRun)
class CatalogSyncRunAdmin(admin.ModelAdmin):
    list_display = ("supplier", "status", "received_count", "created_count", "updated_count", "deactivated_count", "started_at", "finished_at")
    list_filter = ("supplier", "status")
    readonly_fields = ("supplier", "status", "started_at", "finished_at", "received_count", "created_count", "updated_count", "deactivated_count", "error")


@admin.register(CatalogMatchDecision)
class CatalogMatchDecisionAdmin(admin.ModelAdmin):
    list_display = ("product", "decision", "created_by", "is_confirmed", "created_at")
    list_filter = ("decision", "is_confirmed", "product__supplier", "created_by")
    search_fields = ("product__article", "product__name", "session__position_name", "note")
    readonly_fields = ("session", "product", "decision", "reason_codes", "requirement_signature", "created_by", "is_confirmed", "created_at")
