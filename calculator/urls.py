from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="calculator_home"),
    path("<int:pk>/", views.home, name="calculator_estimate"),
]
