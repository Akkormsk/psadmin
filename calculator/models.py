from decimal import Decimal

from django.conf import settings
from django.db import models


class PriceItem(models.Model):
    CATEGORY_PAPER = "paper"
    CATEGORY_KONICA = "konica"
    CATEGORY_XEROX = "xerox"
    CATEGORY_POSTPRESS = "postpress"
    CATEGORY_EMBOSSING = "embossing"
    CATEGORY_CHOICES = [
        (CATEGORY_PAPER, "Бумага"),
        (CATEGORY_KONICA, "Печать · Konica"),
        (CATEGORY_XEROX, "Печать · Xerox"),
        (CATEGORY_POSTPRESS, "Постпечатная обработка"),
        (CATEGORY_EMBOSSING, "Тиснение"),
    ]

    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    name = models.CharField(max_length=200)
    unit_name = models.CharField(max_length=40, default="ед.")
    unit_price = models.DecimalField(max_digits=12, decimal_places=4)
    base_item = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="dependent_items")
    price_multiplier = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("1.0000"))
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category", "name"]
        verbose_name = "Позиция калькулятора"
        verbose_name_plural = "Справочник калькулятора"

    def __str__(self):
        return self.name

    @property
    def effective_unit_price(self):
        if self.base_item_id:
            return self.base_item.effective_unit_price * self.price_multiplier
        return self.unit_price


class CalculatorSettings(models.Model):
    hourly_rate = models.DecimalField("Цена часа", max_digits=12, decimal_places=2, default=Decimal("550.00"))
    material_coefficient = models.DecimalField("Коэффициент материала", max_digits=7, decimal_places=3, default=Decimal("2.000"))
    time_coefficient = models.DecimalField("Коэффициент времени", max_digits=7, decimal_places=3, default=Decimal("1.500"))
    regular_discount = models.DecimalField("Скидка постоянника, %", max_digits=5, decimal_places=2, default=Decimal("10.00"))
    partner_discount = models.DecimalField("Скидка контрагента, %", max_digits=5, decimal_places=2, default=Decimal("15.00"))
    urgency_multiplier = models.DecimalField("Коэффициент без очереди", max_digits=7, decimal_places=3, default=Decimal("1.500"))

    class Meta:
        verbose_name = "Настройки листового калькулятора"
        verbose_name_plural = "Настройки листового калькулятора"

    def __str__(self):
        return "Настройки листового калькулятора"


class Estimate(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calculator_estimates")
    name = models.CharField(max_length=200, default="Новый расчёт")
    product_quantity = models.PositiveIntegerField("Тираж конечного изделия", default=1)
    work_hours = models.DecimalField("Рабочие часы", max_digits=8, decimal_places=1, default=Decimal("0.0"))
    settings_snapshot = models.JSONField(default=dict, blank=True)
    summary_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Расчёт"
        verbose_name_plural = "Сохранённые расчёты"

    def __str__(self):
        return self.name


class EstimateLine(models.Model):
    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name="lines")
    category = models.CharField(max_length=20, choices=PriceItem.CATEGORY_CHOICES)
    price_item = models.ForeignKey(PriceItem, null=True, blank=True, on_delete=models.SET_NULL)
    name_snapshot = models.CharField(max_length=200)
    unit_name = models.CharField(max_length=40, default="ед.")
    unit_price_snapshot = models.DecimalField(max_digits=12, decimal_places=4)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    is_custom = models.BooleanField(default=False)

    class Meta:
        ordering = ["pk"]

    @property
    def total(self):
        return self.unit_price_snapshot * self.quantity
