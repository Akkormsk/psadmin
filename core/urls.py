from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("account/", views.account, name="account"),
    path("account/avatar/", views.avatar, name="account_avatar"),
    path("users/<int:user_id>/avatar/", views.user_avatar, name="user_avatar"),
]
