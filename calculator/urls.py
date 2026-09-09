from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="calculator_home"),
    path("save/", views.save_estimate, name="calculator_estimate_create"),
    path("<int:pk>/save/", views.save_estimate, name="calculator_estimate_save"),
    path("<int:pk>/duplicate/", views.duplicate_estimate, name="calculator_estimate_duplicate"),
    path("<int:pk>/delete/", views.delete_estimate, name="calculator_estimate_delete"),
    path("<int:pk>/", views.home, name="calculator_estimate"),
]
