from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="calculator_home"),
    path("<int:pk>/delete/", views.delete_estimate, name="calculator_estimate_delete"),
    path("<int:pk>/", views.home, name="calculator_estimate"),
]
