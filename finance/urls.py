from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.manager_settings, name="manager_settings"),
    path("calculation/", views.calculation, name="calculation"),
    path("expenses/", views.expenses, name="expenses"),
]
