from django.urls import path

from . import views

app_name = "tender_selection"

urlpatterns = [
    path("", views.tender_list, name="list"),
    path("settings/", views.filter_settings, name="settings"),
    path("pull/", views.pull_now, name="pull"),
    path("<int:pk>/", views.tender_detail, name="detail"),
    path("<int:pk>/doc/<int:idx>/", views.doc_preview, name="doc_preview"),
    path("<int:pk>/review/", views.set_review, name="review"),
    path("<int:pk>/push/", views.push_estimate, name="push"),
    path("<int:pk>/dismiss/", views.dismiss, name="dismiss"),
]
