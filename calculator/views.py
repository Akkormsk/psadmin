from django.shortcuts import render
from django.contrib.auth.decorators import login_required


@login_required
def home(request):
    return render(request, "core/placeholder.html", {"title": "Калькулятор", "message": "Раздел калькулятора находится в разработке."})
