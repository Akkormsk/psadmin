from django import forms

from .models import CanonPriceItem, PriceItem, SheetPriceItem


class BasePriceItemAdminForm(forms.ModelForm):
    allowed_categories = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        labels = dict(PriceItem.CATEGORY_CHOICES)
        self.fields["category"].choices = [(value, labels[value]) for value in self.allowed_categories]


class SheetPriceItemAdminForm(BasePriceItemAdminForm):
    allowed_categories = ("paper", "konica", "xerox", "postpress", "embossing")

    class Meta:
        model = SheetPriceItem
        fields = "__all__"


class CanonPriceItemAdminForm(BasePriceItemAdminForm):
    allowed_categories = ("wide_paper", "wide_print", "wide_postpress")

    class Meta:
        model = CanonPriceItem
        fields = "__all__"
