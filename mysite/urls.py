from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
    path("calculator/", include("calculator.urls")),
    path("cash/", include("cash.urls")),
    path("payroll/", include("payroll.urls")),
]
