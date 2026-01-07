from django.urls import path
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required

@login_required
def home(request):
    return HttpResponse("Зарплата — в разработке")

urlpatterns = [
    path("", home, name="payroll_home"),
]
