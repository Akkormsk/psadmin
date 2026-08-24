from django.contrib import admin

from .models import ProcessDefinition, ProductionTrainingExample, ProductionTrainingSession, ProductionTrainingTurn, ProductionType, TenderEstimate, TenderLine, TenderSettings


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
