from django.contrib import admin
from django.http import HttpResponse
from django.urls import path


def index(request):
    return HttpResponse("Приложение работает! Это главная страница.")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", index, name="index"),
]
