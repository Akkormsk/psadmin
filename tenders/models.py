from decimal import Decimal

from django.conf import settings
from django.db import models


class TenderSettings(models.Model):
    vat_rate = models.DecimalField("НДС, %", max_digits=5, decimal_places=2, default=Decimal("5.00"))

    class Meta:
        verbose_name = "Настройки тендеров"
        verbose_name_plural = "Настройки тендеров"

    def __str__(self):
        return "Настройки тендеров"


class TenderEstimate(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tender_estimates", verbose_name="Ответственный")
    tender_number = models.CharField("Номер тендера", max_length=100)
    name = models.CharField("Название / комментарий", max_length=300)
    reduction_percent = models.DecimalField("Снижение цены, %", max_digits=5, decimal_places=2, default=Decimal("30.00"))
    russia_delivery = models.DecimalField("Доставка по РФ", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    vat_rate_snapshot = models.DecimalField("НДС, %", max_digits=5, decimal_places=2, default=Decimal("5.00"))
    summary_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Просчёт тендера"
        verbose_name_plural = "Просчёты тендеров"

    def __str__(self):
        return f"{self.tender_number} — {self.name}"


class TenderLine(models.Model):
    estimate = models.ForeignKey(TenderEstimate, on_delete=models.CASCADE, related_name="lines")
    name = models.CharField("Наименование", max_length=500)
    quantity = models.DecimalField("Количество", max_digits=14, decimal_places=2)
    nmck_unit = models.DecimalField("НМЦК за единицу", max_digits=14, decimal_places=2)
    material_unit = models.DecimalField("Материал", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    application_unit = models.DecimalField("Нанесение", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    logistics_unit = models.DecimalField("Логистика", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    product_url = models.URLField("Ссылка", max_length=1000, blank=True)
    comment = models.CharField("Комментарий", max_length=500, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "pk"]
        verbose_name = "Товар"
        verbose_name_plural = "Товары"

    def __str__(self):
        return self.name
