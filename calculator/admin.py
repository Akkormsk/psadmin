import json

from django.contrib import admin
from django.db import transaction
from django.http import JsonResponse
from django.urls import path

from .forms import CanonPriceItemAdminForm, SheetPriceItemAdminForm
from .models import CalculatorSettings, CanonPriceItem, Estimate, EstimateLine, PriceItem, SheetPriceItem


class PriceItemAdminBase(admin.ModelAdmin):
    change_list_template = "admin/calculator/priceitem/change_list.html"
    list_display = ("drag_handle", "name", "category", "unit_price", "is_active", "unit_name")
    list_display_links = ("name",)
    list_editable = ("unit_price", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("name",)
    ordering = ("category", "sort_order", "pk")
    list_per_page = 200
    categories = ()

    @admin.display(description="")
    def drag_handle(self, obj):
        return "⠿"

    def get_queryset(self, request):
        return super().get_queryset(request).filter(category__in=self.categories)

    def get_urls(self):
        custom = [path("reorder/", self.admin_site.admin_view(self.reorder_view), name=f"calculator_{self.model._meta.model_name}_reorder")]
        return custom + super().get_urls()

    def reorder_view(self, request):
        if request.method != "POST":
            return JsonResponse({"ok": False}, status=405)
        try:
            ids = [int(value) for value in json.loads(request.body).get("ids", [])]
        except (TypeError, ValueError, json.JSONDecodeError):
            return JsonResponse({"ok": False}, status=400)
        items = list(PriceItem.objects.filter(pk__in=ids, category__in=self.categories))
        if len(items) != len(ids) or len({item.category for item in items}) > 1:
            return JsonResponse({"ok": False}, status=400)
        item_map = {item.pk: item for item in items}
        with transaction.atomic():
            for position, item_id in enumerate(ids, start=1):
                item = item_map[item_id]
                item.sort_order = position * 10
                item.save(update_fields=["sort_order"])
        return JsonResponse({"ok": True})


@admin.register(SheetPriceItem)
class SheetPriceItemAdmin(PriceItemAdminBase):
    form = SheetPriceItemAdminForm
    categories = SheetPriceItemAdminForm.allowed_categories


@admin.register(CanonPriceItem)
class CanonPriceItemAdmin(PriceItemAdminBase):
    form = CanonPriceItemAdminForm
    categories = CanonPriceItemAdminForm.allowed_categories


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
