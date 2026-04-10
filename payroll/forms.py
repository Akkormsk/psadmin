from django import forms

from .models import OrderRecord


class OrderRecordCreateForm(forms.ModelForm):
    accounting_period = forms.ChoiceField(label="Учетный период")

    class Meta:
        model = OrderRecord
        fields = ["order_number", "gross_profit", "accounting_period"]
        widgets = {
            "order_number": forms.TextInput(
                attrs={
                    "inputmode": "numeric",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        year = self._current_year()
        self.fields["accounting_period"].choices = self._month_choices_for_year(year)
        self.fields["accounting_period"].initial = self._current_period_value()

    def clean_order_number(self):
        order_number = self.cleaned_data["order_number"]

        if not order_number.isdigit():
            raise forms.ValidationError("Номер заказа должен содержать только цифры")

        return order_number

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