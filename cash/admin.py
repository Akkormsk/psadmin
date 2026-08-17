from django.contrib import admin

from .models import CashAuditLog, CashReconciliation, CashTransaction


@admin.register(CashTransaction)
class CashTransactionAdmin(admin.ModelAdmin):
    list_display = ("operation_date", "account", "direction", "amount", "reason", "created_by", "updated_at")
    list_filter = ("account", "direction", "operation_date")
    search_fields = ("reason", "created_by__username")


@admin.register(CashReconciliation)
class CashReconciliationAdmin(admin.ModelAdmin):
    list_display = ("effective_date", "cash_balance", "card_balance", "created_by", "created_at")
    search_fields = ("note",)


@admin.register(CashAuditLog)
class CashAuditLogAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "actor", "action", "message")
    list_filter = ("action", "occurred_at")
    search_fields = ("message", "actor__username")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
