from django.urls import path
from django.http import HttpResponse

def home(request):
    return HttpResponse("Зарплата — в разработке")

urlpatterns = [
    path("", home, name="payroll_home"),
]
