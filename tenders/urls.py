from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="tender_home"),
    path("import/preview/", views.import_preview, name="tender_import_preview"),
    path("import/ai/", views.ai_import_preview, name="tender_ai_import_preview"),
    path("<int:pk>/", views.home, name="tender_estimate"),
    path("<int:pk>/delete/", views.delete_estimate, name="tender_estimate_delete"),
]
