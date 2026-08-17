from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="cash_home"),
    path("operations/new/", views.transaction_create, name="transaction_create"),
    path("operations/<int:pk>/edit/", views.transaction_update, name="transaction_update"),
    path("operations/<int:pk>/delete/", views.transaction_delete, name="transaction_delete"),
    path("reconcile/", views.reconcile, name="reconcile"),
    path("history/", views.audit_log, name="audit_log"),
]
