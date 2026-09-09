from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render
from django.utils import timezone

from finance.views import _period_options, period_financials


@login_required
@user_passes_test(lambda user: user.is_superuser)
def financial_accounting(request):
    code = request.GET.get("period") or timezone.localdate().strftime("%Y-%m")
    context = period_financials(code)
    context["period_options"] = _period_options(code)
    return render(request, "payroll/financial_accounting.html", context)
