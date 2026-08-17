from django import forms

from .models import CashReconciliation, CashTransaction


class CashTransactionForm(forms.ModelForm):
    class Meta:
        model = CashTransaction
        fields = ("operation_date", "account", "direction", "amount", "reason")
        widgets = {
            "operation_date": forms.DateInput(attrs={"type": "date"}),
            "amount": forms.NumberInput(attrs={"min": "0.01", "step": "0.01", "inputmode": "decimal"}),
            "reason": forms.TextInput(attrs={"placeholder": "Например: заказ №26573"}),
        }


class CashReconciliationForm(forms.ModelForm):
    class Meta:
        model = CashReconciliation
        fields = ("effective_date", "cash_balance", "card_balance", "note")
        widgets = {
            "effective_date": forms.DateInput(attrs={"type": "date"}),
            "cash_balance": forms.NumberInput(attrs={"min": "0", "step": "0.01", "inputmode": "decimal"}),
            "card_balance": forms.NumberInput(attrs={"min": "0", "step": "0.01", "inputmode": "decimal"}),
            "note": forms.TextInput(attrs={"placeholder": "Например: сверка после переноса базы"}),
        }

    def validate_unique(self):
        """A later reconciliation on the same date intentionally replaces the prior one."""
        pass
