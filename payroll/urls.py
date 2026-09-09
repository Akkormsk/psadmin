from django.urls import path

from . import views

app_name = "payroll"

urlpatterns = [
    path("", views.financial_accounting, name="index"),
]
