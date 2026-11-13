from django.urls import path, include
from django.contrib import admin
from tenders import debug_tree

# from mysite.adminsite import admin_site  # <-- наш сайт админки

urlpatterns = [
    # path("admin/", admin_site.urls),
    path("admin/", admin.site.urls),
    path("debug/tender-tree/<int:pk>/", debug_tree.tender_tree_debug, name="tender_tree_debug"),
    path("", include("core.urls")),
    path("calculator/", include("calculator.urls")),
    path("cash/", include("cash.urls")),
    path("payroll/", include("payroll.urls")),
    path("finance/", include("finance.urls")),
    path("tenders/", include("tenders.urls")),
    path("tender-selection/", include("tender_selection.urls")),
    path("", include("django.contrib.auth.urls")),  # login/logout
]
