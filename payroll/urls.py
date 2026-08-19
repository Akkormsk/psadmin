from django.urls import path

from . import views

app_name = "payroll"

urlpatterns = [
    path("", views.orderrecord_list, name="index"),
    path("orders/", views.orderrecord_list, name="orderrecord_list"),
    path("orders/create/", views.orderrecord_create, name="orderrecord_create"),
    path("orders/<int:pk>/edit/", views.orderrecord_update, name="orderrecord_update"),
    path("orders/<int:pk>/delete/", views.orderrecord_delete, name="orderrecord_delete"),
]
