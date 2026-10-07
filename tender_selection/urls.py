from django.urls import path

from . import views

app_name = "tender_selection"

urlpatterns = [
    path("", views.tender_list, name="list"),
    path("settings/", views.filter_settings, name="settings"),
    path("evaluation-settings/", views.evaluation_settings, name="evaluation_settings"),
    path("word-audit/", views.word_audit_page, name="word_audit"),
    path("word-audit/run/", views.word_audit_run, name="word_audit_run"),
    path("word-audit/apply/", views.word_audit_apply, name="word_audit_apply"),
    path("profile-triage/", views.profile_triage_run, name="profile_triage_run"),
    path("pull/", views.pull_now, name="pull"),
    path("<int:pk>/questions/<int:interaction_pk>/answer/", views.owner_interaction_answer, name="owner_interaction_answer"),
    path("<int:pk>/", views.tender_detail, name="detail"),
    path("<int:pk>/risk/", views.risk_status, name="risk_status"),
    path("<int:pk>/doc/<int:idx>/", views.doc_preview, name="doc_preview"),
    path("<int:pk>/doc/<int:idx>/upload/", views.doc_upload, name="doc_upload"),
    path("<int:pk>/doc/<int:idx>/zip/<path:entry>/", views.doc_zip_entry, name="doc_zip_entry"),
    path("diag/eis/", views.eis_diag, name="eis_diag"),
    path("<int:pk>/review/", views.set_review, name="review"),
    path("<int:pk>/push/", views.push_estimate, name="push"),
    path("<int:pk>/dismiss/", views.dismiss, name="dismiss"),
    path("<int:pk>/restore/", views.restore, name="restore"),
    path("<int:pk>/forecast/", views.toggle_forecast, name="toggle_forecast"),
    path("archive/", views.archive, name="archive"),
    path("estimate/<int:pk>/outcome/", views.enter_outcome, name="enter_outcome"),
    path("estimate/<int:pk>/bid/", views.save_bid, name="save_bid"),
    path("estimate/<int:pk>/dismiss/", views.dismiss_estimate, name="dismiss_estimate"),
    path("estimate/<int:pk>/restore/", views.restore_estimate, name="restore_estimate"),
]
