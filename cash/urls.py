from django.urls import path
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required

@login_required
def home(request):
    return HttpResponse("Касса — в разработке")

urlpatterns = [
    path("", home, name="cash_home"),
]
