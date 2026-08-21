from django import forms
from django.contrib.auth import get_user_model

from .models import OrderRecord


class EmployeeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.get_full_name().strip() or "Имя не указано"


class OrderRecordCreateForm(forms.ModelForm):
    accounting_period = forms.ChoiceField(label="Учетный период")

    class Meta:
        model = OrderRecord
        fields = ["record_type", "order_number", "gross_profit", "accounting_period"]
        labels = {"order_number": "Номер заказа", "gross_profit": "Сумма"}
        widgets = {
            "record_type": forms.RadioSelect,
            "order_number": forms.TextInput(
                attrs={
                    "inputmode": "numeric",
                }
            ),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)

        year = self._current_year()
        self.fields["accounting_period"].choices = self._month_choices_for_year(year)
        self.fields["accounting_period"].initial = self._current_period_value()
        if user and user.is_superuser:
            self.fields["manager"] = EmployeeChoiceField(
                label="Ответственный",
                queryset=get_user_model().objects.filter(is_active=True, is_superuser=False).order_by(
                    "first_name", "last_name", "pk"
                ),
                initial=self.instance.manager_id if self.instance and self.instance.pk else None,
            )
        record_type = (
            self.data.get(self.add_prefix("record_type"))
            if self.is_bound
            else getattr(self.instance, "record_type", OrderRecord.RECORD_ORDER)
        )
        if record_type == OrderRecord.RECORD_DESIGN:
            self.fields["order_number"].widget.attrs.update({"inputmode": "text", "placeholder": "Например: макет вывески"})
        else:
            self.fields["order_number"].widget.attrs.update({"inputmode": "numeric", "placeholder": "Только цифры"})

    def clean(self):
        cleaned_data = super().clean()
        order_number = (cleaned_data.get("order_number") or "").strip()
        record_type = cleaned_data.get("record_type")
        if record_type == OrderRecord.RECORD_ORDER and order_number and not order_number.isascii():
            self.add_error("order_number", "Номер заказа должен содержать только цифры")
        elif record_type == OrderRecord.RECORD_ORDER and order_number and not order_number.isdigit():
            self.add_error("order_number", "Номер заказа должен содержать только цифры")
        cleaned_data["order_number"] = order_number
        return cleaned_data

    @staticmethod
    def _current_year() -> int:
        from django.utils import timezone

        return timezone.localdate().year

    @staticmethod
    def _current_period_value() -> str:
        from django.utils import timezone

        d = timezone.localdate()
        return f"{d.year:04d}-{d.month:02d}"

    @staticmethod
    def _month_choices_for_year(year: int):
        # Russian month names in nominative form, matching UI request.
        month_names = [
            "Январь",
            "Февраль",
            "Март",
            "Апрель",
            "Май",
            "Июнь",
            "Июль",
            "Август",
            "Сентябрь",
            "Октябрь",
            "Ноябрь",
            "Декабрь",
        ]
        return [
            (f"{year:04d}-{month:02d}", f"{month_names[month - 1]} {year}")
            for month in range(1, 13)
        ]
