from django.contrib import admin

from .models import AttributeConceptHint, CascadeConfigVersion, CascadeLabPreset, CatalogCategory, CatalogMatchDecision, CatalogProduct, CatalogSupplier, CatalogSyncRun, Counterparty, Lesson, ProcessDefinition, ProductionTrainingExample, ProductionTrainingSession, ProductionTrainingTurn, ProductionType, Proposal, StageCounterpartyLink, TenderEstimate, TenderKnowledgeSource, TenderLine, TenderSettings, UnitAlias


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


@admin.register(UnitAlias)
class UnitAliasAdmin(admin.ModelAdmin):
    list_display = ("spelling", "canonical", "created_at")
    search_fields = ("spelling", "canonical")
    ordering = ("canonical", "spelling")


@admin.register(AttributeConceptHint)
class AttributeConceptHintAdmin(admin.ModelAdmin):
    list_display = ("concept_key", "attribute_name", "hits", "updated_at")
    search_fields = ("concept_key", "attribute_name")
    ordering = ("-hits", "-updated_at")


@admin.register(TenderSettings)
class TenderSettingsAdmin(admin.ModelAdmin):
    list_display = ("vat_rate", "roi_good_percent", "roi_thin_percent", "auto_start_product_search", "auto_recalculate_requirements")

    def has_add_permission(self, request):
        return not TenderSettings.objects.exists()


@admin.register(CascadeLabPreset)
class CascadeLabPresetAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "updated_at")


@admin.register(CascadeConfigVersion)
class CascadeConfigVersionAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "created_by", "created_at")


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
    list_display = ("name", "role", "supplies_input", "performs_production", "terminal_mode", "is_active")
    list_filter = ("role", "terminal_mode", "is_active")
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
    list_display = ("title", "supplier_name", "counterparty", "source_type", "updated_at", "is_active")
    list_filter = ("source_type", "is_active", "created_by")
    search_fields = ("title", "supplier_name", "url", "content_summary")
    list_editable = ("is_active",)
    # BinaryField не рендерится в форме админки — файл виден только через raw_file_name/тип.
    exclude = ("raw_file",)
    readonly_fields = ("raw_file_name", "raw_file_content_type")


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


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ("summary", "scope", "item_word", "production_type", "is_active", "created_by", "created_at")
    list_filter = ("scope", "is_active", "created_by")
    search_fields = ("summary", "admin_text", "item_word")
    list_editable = ("is_active",)
    readonly_fields = ("session", "tz_labels", "outcome", "created_at", "updated_at")


@admin.register(CatalogMatchDecision)
class CatalogMatchDecisionAdmin(admin.ModelAdmin):
    list_display = ("product", "decision", "created_by", "is_confirmed", "created_at")
    list_filter = ("decision", "is_confirmed", "product__supplier", "created_by")
    search_fields = ("product__article", "product__name", "session__position_name", "note")
    readonly_fields = ("session", "product", "decision", "reason_codes", "requirement_signature", "created_by", "is_confirmed", "created_at")


@admin.register(Counterparty)
class CounterpartyAdmin(admin.ModelAdmin):
    list_display = ("name", "catalog_supplier", "is_active", "updated_at")
    list_filter = ("is_active",)
    list_editable = ("is_active",)
    search_fields = ("name", "notes")


@admin.register(StageCounterpartyLink)
class StageCounterpartyLinkAdmin(admin.ModelAdmin):
    list_display = ("stage", "counterparty", "price_source_type", "priority", "is_active")
    list_filter = ("price_source_type", "is_active", "stage")
    list_editable = ("is_active", "priority")
    search_fields = ("stage__name", "counterparty__name")


@admin.register(Proposal)
class ProposalAdmin(admin.ModelAdmin):
    list_display = ("summary", "type", "status", "batch_id", "created_by", "created_at")
    list_filter = ("type", "status")
    search_fields = ("summary", "source_text")
    readonly_fields = ("batch_id", "payload", "session", "source_text", "created_by", "created_at")
