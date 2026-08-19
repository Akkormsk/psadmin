from django.contrib import admin

from .models import CalculatorSettings, Estimate, EstimateLine, PriceItem


@admin.register(PriceItem)
class PriceItemAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "unit_name", "unit_price", "base_item", "price_multiplier", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("name",)


@admin.register(CalculatorSettings)
class CalculatorSettingsAdmin(admin.ModelAdmin):
    list_display = ("hourly_rate", "material_coefficient", "time_coefficient", "regular_discount", "partner_discount", "urgency_multiplier")

    def has_add_permission(self, request):
        return not CalculatorSettings.objects.exists()


class EstimateLineInline(admin.TabularInline):
    model = EstimateLine
    extra = 0
    readonly_fields = ("name_snapshot", "unit_price_snapshot")


@admin.register(Estimate)
class EstimateAdmin(admin.ModelAdmin):
    list_display = ("name", "calculator_type", "owner", "product_quantity", "work_hours", "updated_at")
    list_filter = ("calculator_type", "owner")
    search_fields = ("name",)
    inlines = (EstimateLineInline,)
