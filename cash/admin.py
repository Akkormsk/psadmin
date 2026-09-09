from django.contrib import admin

from .models import BankPayment, BankSyncState, CashAuditLog, CashReconciliation, CashTransaction


@admin.register(BankPayment)
class BankPaymentAdmin(admin.ModelAdmin):
    list_display = ("operation_date", "amount", "currency", "counterparty_name", "status", "hidden_from_managers")
    list_filter = ("hidden_from_managers", "status", "operation_date")
    search_fields = ("counterparty_name", "counterparty_inn", "payment_purpose", "doc_number", "external_id")
    actions = ("hide_from_managers", "show_to_managers")
    readonly_fields = ("external_id", "raw", "created_at", "updated_at")

    @admin.action(description="Скрыть от менеджеров")
    def hide_from_managers(self, request, queryset):
        queryset.update(hidden_from_managers=True)

    @admin.action(description="Показать менеджерам")
    def show_to_managers(self, request, queryset):
        queryset.update(hidden_from_managers=False)


@admin.register(BankSyncState)
class BankSyncStateAdmin(admin.ModelAdmin):
    list_display = ("last_synced_at", "last_status")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
