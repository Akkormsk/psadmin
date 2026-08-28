from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="tender_home"),
    path("knowledge/sync/", views.knowledge_sync, name="tender_knowledge_sync"),
    path("catalog/gifts/import-test/", views.gifts_import_test, name="gifts_import_test"),
    path("import/preview/", views.import_preview, name="tender_import_preview"),
    path("import/ai/", views.ai_import_preview, name="tender_ai_import_preview"),
    path("documents/inspect/", views.document_inspect, name="tender_document_inspect"),
    path("documents/analyze/", views.document_preview, name="tender_document_preview"),
    path("import/requirements/", views.technical_requirements_preview, name="tender_requirements_preview"),
    path("production/route/", views.production_route_preview, name="tender_production_route_preview"),
    path("production/revise/", views.revise_production_hypothesis, name="tender_revise_production_hypothesis"),
    path("production/catalog/select/", views.select_catalog_product, name="tender_select_catalog_product"),
    path("production/source/", views.add_calculation_source, name="tender_add_calculation_source"),
    path("production/confirm/", views.confirm_production_type, name="tender_confirm_production_type"),
    path("production/knowledge/", views.calculator_knowledge_proposal, name="tender_calculator_knowledge_proposal"),
    path("<int:pk>/", views.home, name="tender_estimate"),
    path("<int:pk>/delete/", views.delete_estimate, name="tender_estimate_delete"),
]
