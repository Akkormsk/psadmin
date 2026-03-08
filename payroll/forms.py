from django import forms

from .models import OrderRecord


class OrderRecordCreateForm(forms.ModelForm):
    class Meta:
        model = OrderRecord
        fields = ["order_number", "gross_profit"]
        widgets = {
            "order_number": forms.TextInput(
                attrs={
                    "inputmode": "numeric",
                }
            ),
        }

    def clean_order_number(self):
        order_number = self.cleaned_data["order_number"]

        if not order_number.isdigit():
            raise forms.ValidationError("Номер заказа должен содержать только цифры")

        return order_number