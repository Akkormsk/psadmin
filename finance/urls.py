from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.manager_settings, name="manager_settings"),
    path("calculation/", views.calculation, name="calculation"),
    path("expenses/", views.expenses, name="expenses"),
    path("orders/create/", views.orders_create, name="orders_create"),
    path("orders/<int:pk>/edit/", views.orders_update, name="orders_update"),
    path("orders/<int:pk>/delete/", views.orders_delete, name="orders_delete"),
]
