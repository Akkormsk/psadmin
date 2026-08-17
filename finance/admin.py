from django.contrib import admin

from .models import PayrollLine


@admin.register(PayrollLine)
class PayrollLineAdmin(admin.ModelAdmin):
    list_display = ("period", "name", "kind", "design_amount", "design_percent", "deductions", "advance")
    list_filter = ("period", "kind")
    search_fields = ("name", "manager__username")
    fields = (
        "period",
        "kind",
        "manager",
        "name",
        "work_shifts",
        "leave_shifts",
        "shift_rate",
        "leave_shift_rate",
        "fixed_salary",
        "design_amount",
        "design_percent",
        "deductions",
        "advance",
        "order_profit",
        "kpi_percent",
        "kpi_bonus",
    )
