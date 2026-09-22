from django.urls import path

from . import views

app_name = "tender_selection"

urlpatterns = [
    path("", views.tender_list, name="list"),
    path("settings/", views.filter_settings, name="settings"),
    path("words/", views.save_words, name="save_words"),
    path("pull/", views.pull_now, name="pull"),
    path("<int:pk>/", views.tender_detail, name="detail"),
    path("<int:pk>/risk/", views.risk_status, name="risk_status"),
    path("<int:pk>/doc/<int:idx>/", views.doc_preview, name="doc_preview"),
    path("<int:pk>/doc/<int:idx>/upload/", views.doc_upload, name="doc_upload"),
    path("<int:pk>/doc/<int:idx>/zip/<path:entry>/", views.doc_zip_entry, name="doc_zip_entry"),
    path("diag/eis/", views.eis_diag, name="eis_diag"),
    path("<int:pk>/review/", views.set_review, name="review"),
    path("<int:pk>/push/", views.push_estimate, name="push"),
    path("<int:pk>/dismiss/", views.dismiss, name="dismiss"),
    path("archive/", views.archive, name="archive"),
    path("estimate/<int:pk>/outcome/", views.enter_outcome, name="enter_outcome"),
    path("estimate/<int:pk>/dismiss/", views.dismiss_estimate, name="dismiss_estimate"),
]
