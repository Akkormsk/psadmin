from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .forms import OrderRecordCreateForm
from .models import OrderRecord


@login_required
def orderrecord_list(request, form=None, open_modal=False):
    records = OrderRecord.objects.filter(manager=request.user).order_by("-created_at")

    if form is None:
        form = OrderRecordCreateForm()

    return render(
        request,
        "payroll/orderrecord_list.html",
        {
            "records": records,
            "form": form,
            "open_modal": open_modal,
        },
    )


@login_required
def orderrecord_create(request):
    if request.method != "POST":
        return redirect("payroll:orderrecord_list")

    form = OrderRecordCreateForm(request.POST)

    if form.is_valid():
        record = form.save(commit=False)
        record.manager = request.user
        record.created_by = request.user
        record.source = OrderRecord.SOURCE_MANUAL
        record.save()

        duplicate_exists = OrderRecord.objects.filter(
            order_number=record.order_number
        ).exclude(pk=record.pk).exists()

        if duplicate_exists:
            messages.warning(
                request,
                f"Order number {record.order_number} already exists in the system."
            )

        return redirect("payroll:orderrecord_list")

    records = OrderRecord.objects.filter(manager=request.user).order_by("-created_at")
    return render(
        request,
        "payroll/orderrecord_list.html",
        {
            "records": records,
            "form": form,
            "open_modal": True,
        },
    )