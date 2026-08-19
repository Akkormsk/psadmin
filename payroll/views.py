from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from .forms import OrderRecordCreateForm
from .models import OrderRecord


@login_required
def orderrecord_list(request, form=None, open_modal=False, edit_form=None, open_edit_id=None):
    is_admin_view = request.user.is_superuser
    selected_manager = ""
    selected_period = timezone.localdate().strftime("%Y-%m")
    managers = []
    periods = []

    if is_admin_view:
        records = OrderRecord.objects.select_related("manager", "manager__profile", "created_by", "created_by__profile").defer(
            "manager__profile__avatar_data", "created_by__profile__avatar_data"
        )
        managers = get_user_model().objects.filter(is_active=True, is_superuser=False).order_by("first_name", "last_name", "pk")
        period_values = list(
            OrderRecord.objects.order_by()
            .values_list("accounting_period", flat=True)
            .distinct()
            .order_by("-accounting_period")
        )
        periods = [
            {
                "value": period,
                "label": OrderRecord(accounting_period=period).accounting_period_ru,
            }
            for period in period_values
        ]

        selected_manager = request.GET.get("manager", "")
        selected_period = request.GET.get("period", selected_period)

        if selected_manager.isdigit():
            records = records.filter(manager_id=int(selected_manager))
        else:
            selected_manager = ""

        if selected_period in period_values:
            records = records.filter(accounting_period=selected_period)
        else:
            selected_period = ""

        total_gross_profit = records.aggregate(total=Sum("gross_profit"))["total"] or 0
        records = list(records.order_by("-created_at"))
    else:
        manager_records = OrderRecord.objects.filter(manager=request.user)
        period_values = list(
            manager_records.order_by()
            .values_list("accounting_period", flat=True)
            .distinct()
            .order_by("-accounting_period")
        )
        selected_period = request.GET.get("period", selected_period)
        if selected_period not in period_values:
            selected_period = period_values[0] if period_values else selected_period
        periods = [
            {
                "value": period,
                "label": OrderRecord(accounting_period=period).accounting_period_ru,
            }
            for period in period_values
        ]
        records = list(manager_records.filter(accounting_period=selected_period).select_related("manager", "manager__profile", "created_by", "created_by__profile").defer(
            "manager__profile__avatar_data", "created_by__profile__avatar_data"
        ).order_by("-created_at"))
        total_gross_profit = None

    if form is None:
        form = OrderRecordCreateForm(user=request.user)
    for record in records:
        record.edit_form = (
            edit_form
            if edit_form is not None and record.pk == open_edit_id
            else OrderRecordCreateForm(instance=record, user=request.user, prefix=f"edit-{record.pk}")
        )

    return render(
        request,
        "payroll/orderrecord_list.html",
        {
            "records": records,
            "form": form,
            "open_modal": open_modal,
            "is_admin_view": is_admin_view,
            "managers": managers,
            "periods": periods,
            "selected_manager": selected_manager,
            "selected_period": selected_period,
            "total_gross_profit": total_gross_profit,
            "open_edit_id": open_edit_id,
        },
    )


@login_required
def orderrecord_create(request):
    if request.method != "POST":
        return redirect("payroll:orderrecord_list")

    form = OrderRecordCreateForm(request.POST, user=request.user)

    if form.is_valid():
        record = form.save(commit=False)
        record.manager = form.cleaned_data.get("manager") if request.user.is_superuser else request.user
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

    return orderrecord_list(request, form=form, open_modal=True)


@login_required
def orderrecord_update(request, pk):
    records = OrderRecord.objects.all()
    if not request.user.is_superuser:
        records = records.filter(manager=request.user)
    record = records.filter(pk=pk).first()
    if record is None or request.method != "POST":
        return redirect("payroll:orderrecord_list")

    form = OrderRecordCreateForm(
        request.POST,
        instance=record,
        user=request.user,
        prefix=f"edit-{record.pk}",
    )
    if form.is_valid():
        record = form.save(commit=False)
        if request.user.is_superuser:
            record.manager = form.cleaned_data["manager"]
        record.save()
        messages.success(request, "Запись сохранена.")
        return redirect("payroll:orderrecord_list")

    return orderrecord_list(request, edit_form=form, open_edit_id=record.pk)

@login_required
def orderrecord_delete(request, pk):
    if request.method != "POST":
        return redirect("payroll:orderrecord_list")

    records = OrderRecord.objects.filter(pk=pk)
    if not request.user.is_superuser:
        records = records.filter(manager=request.user)

    record = records.first()

    if record:
        record.delete()
        # messages.success(request, "Запись удалена")

    return redirect("payroll:orderrecord_list")
