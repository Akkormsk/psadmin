from django.urls import path
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required

@login_required
def home(request):
    return HttpResponse("Калькулятор — в разработке")

urlpatterns = [
    path("", home, name="calculator_home"),
]
