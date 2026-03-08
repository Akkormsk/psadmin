from django.contrib import admin

from .models import OrderRecord


@admin.register(OrderRecord)
class OrderRecordAdmin(admin.ModelAdmin):
    list_display = (
        "order_number",
        "gross_profit",
        "manager",
        "created_by",
        "source",
        "created_at",
        "updated_at",
    )
    list_filter = ("source", "created_at", "updated_at", "manager")
    search_fields = ("order_number", "manager__username", "created_by__username")
    readonly_fields = ("created_at", "updated_at")