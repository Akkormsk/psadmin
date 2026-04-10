from django.conf import settings
from django.db import models


def current_accounting_period() -> str:
    from django.utils import timezone

    d = timezone.localdate()
    return f"{d.year:04d}-{d.month:02d}"


class OrderRecord(models.Model):
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
        return f"Order {self.order_number} - {self.manager.username} - {self.gross_profit}"
