from django.contrib import admin
from django.utils.html import format_html

from .models import OrderRecord


@admin.register(OrderRecord)
class OrderRecordAdmin(admin.ModelAdmin):
    list_display = (
        "order_number",
        "gross_profit_rub",
        "accounting_period_ru",
        "manager",
        "created_by",
        "source",
        "created_at",
        "updated_at",
    )
    list_filter = ("source", "accounting_period", "created_at", "updated_at", "manager")
    search_fields = ("order_number", "manager__username", "created_by__username")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Валовая прибыль")
    def gross_profit_rub(self, obj: OrderRecord):
        # Format in admin without relying on template filters.
        try:
            value = f"{obj.gross_profit:,.2f}".replace(",", " ").replace(".", ",")
        except Exception:
            return obj.gross_profit
        return format_html("{} руб.", value)