from django.urls import path
from django.http import HttpResponse


def home(request):
    return HttpResponse("Калькулятор — в разработке")


urlpatterns = [
    path("", home, name="calculator_home"),
]
