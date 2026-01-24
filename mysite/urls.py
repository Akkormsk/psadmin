from django.urls import path, include
from django.contrib import admin
# from mysite.adminsite import admin_site  # <-- наш сайт админки

urlpatterns = [
    # path("admin/", admin_site.urls),
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
    path("calculator/", include("calculator.urls")),
    path("cash/", include("cash.urls")),
    path("payroll/", include("payroll.urls")),
    path("", include("django.contrib.auth.urls")),  # login/logout
]
