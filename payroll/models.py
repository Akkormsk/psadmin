from django.conf import settings
from django.db import models


def current_accounting_period() -> str:
    from django.utils import timezone

    d = timezone.localdate()
    return f"{d.year:04d}-{d.month:02d}"


class OrderRecord(models.Model):
    RECORD_ORDER = "order"
    RECORD_DESIGN = "design"
    RECORD_TYPE_CHOICES = [
        (RECORD_ORDER, "Заказ"),
        (RECORD_DESIGN, "Макет"),
    ]

    SOURCE_MANUAL = "manual"
    SOURCE_CRM_API = "crm_api"
    SOURCE_IMPORT = "import"

    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_CRM_API, "CRM API"),
        (SOURCE_IMPORT, "Import"),
    ]

    order_number = models.CharField(max_length=100)
    gross_profit = models.DecimalField(max_digits=12, decimal_places=2)
    record_type = models.CharField(
        "Тип записи",
        max_length=10,
        choices=RECORD_TYPE_CHOICES,
        default=RECORD_ORDER,
        db_index=True,
    )
    # Stores accounting period as "YYYY-MM" (e.g. "2026-04") for KPI calculations.
    accounting_period = models.CharField(
        max_length=7,
        db_index=True,
        default=current_accounting_period,
    )
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="managed_order_records",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="created_order_records",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_MANUAL,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        responsible = self.manager.get_full_name().strip() or "Имя не указано"
        return f"{self.order_number} — {responsible} — {self.gross_profit}"

    @property
    def accounting_period_ru(self) -> str:
        # "YYYY-MM" -> "Апрель 2026"
        try:
            year_s, month_s = (self.accounting_period or "").split("-")
            year = int(year_s)
            month = int(month_s)
        except Exception:
            return self.accounting_period

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
        if 1 <= month <= 12:
            return f"{month_names[month - 1]} {year}"

        return self.accounting_period
