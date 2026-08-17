from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="cash_home"),
]
