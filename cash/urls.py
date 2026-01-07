from django.urls import path
from django.http import HttpResponse

def home(request):
    return HttpResponse("Касса — в разработке")

urlpatterns = [
    path("", home, name="cash_home"),
]
