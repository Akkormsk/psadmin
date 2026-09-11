from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="cash_home"),
    path("operations/new/", views.transaction_create, name="transaction_create"),
    path("operations/<int:pk>/edit/", views.transaction_update, name="transaction_update"),
    path("operations/<int:pk>/delete/", views.transaction_delete, name="transaction_delete"),
    path("reconcile/", views.reconcile, name="reconcile"),
    path("history/", views.audit_log, name="audit_log"),
    path("bank/sync/", views.bank_sync_now, name="bank_sync_now"),
    path("bank/webhook/", views.bank_webhook, name="bank_webhook"),
    path("bank/<int:pk>/visibility/", views.bank_payment_toggle, name="bank_payment_toggle"),
]
