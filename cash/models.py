from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class CashReconciliation(models.Model):
    """A verified opening balance used as an anchor for all later daily balances."""

    effective_date = models.DateField("Дата сверки", unique=True)
    cash_balance = models.DecimalField(
        "Наличные на утро",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    card_balance = models.DecimalField(
        "Карта на утро",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    note = models.CharField("Комментарий", max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cash_reconciliations",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-effective_date",)
        verbose_name = "Сверка остатков"
        verbose_name_plural = "Сверки остатков"

    def __str__(self):
        return f"Сверка на {self.effective_date:%d.%m.%Y}"


class CashTransaction(models.Model):
    ACCOUNT_CASH = "cash"
    ACCOUNT_CARD = "card"
    ACCOUNT_CHOICES = (
        (ACCOUNT_CASH, "Наличные"),
        (ACCOUNT_CARD, "Карта"),
    )

    DIRECTION_INCOME = "income"
    DIRECTION_EXPENSE = "expense"
    DIRECTION_CHOICES = (
        (DIRECTION_INCOME, "Приход"),
        (DIRECTION_EXPENSE, "Расход"),
    )

    operation_date = models.DateField("Дата операции", db_index=True)
    account = models.CharField("Счёт", max_length=10, choices=ACCOUNT_CHOICES)
    direction = models.CharField("Тип операции", max_length=10, choices=DIRECTION_CHOICES)
    amount = models.DecimalField(
        "Сумма",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
    )
    reason = models.CharField("Основание / № заказа", max_length=255)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cash_transactions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at", "pk")
        verbose_name = "Операция кассы"
        verbose_name_plural = "Операции кассы"

    def __str__(self):
        return f"{self.get_account_display()} · {self.get_direction_display()} · {self.amount}"


class BankPayment(models.Model):
    """Входящий платёж из Модульбанка — зеркало транзакции (operation-history / веб-хук).

    Строки создаёт только синхронизация с банком, руками их не заводят и не удаляют.
    Админ может скрыть платёж от менеджеров флагом ``hidden_from_managers``.
    """

    external_id = models.CharField("ID операции в банке", max_length=64, unique=True)
    status = models.CharField("Статус в банке", max_length=32, blank=True)
    direction = models.CharField("Направление", max_length=16, blank=True)
    amount = models.DecimalField("Сумма", max_digits=14, decimal_places=2)
    currency = models.CharField("Валюта", max_length=3, default="RUR")
    counterparty_name = models.CharField("Плательщик", max_length=255, blank=True)
    counterparty_inn = models.CharField("ИНН плательщика", max_length=20, blank=True)
    counterparty_account = models.CharField("Счёт плательщика", max_length=34, blank=True)
    counterparty_bank = models.CharField("Банк плательщика", max_length=255, blank=True)
    payment_purpose = models.TextField("Назначение платежа", blank=True)
    doc_number = models.CharField("№ документа", max_length=32, blank=True)
    account_number = models.CharField("Счёт зачисления", max_length=34, blank=True)
    executed_at = models.DateTimeField("Дата проведения", null=True, blank=True)
    operation_date = models.DateField("Дата операции", db_index=True)
    hidden_from_managers = models.BooleanField("Скрыт от менеджеров", default=False)
    raw = models.JSONField("Ответ банка", default=dict, blank=True)
    created_at = models.DateTimeField("Загружен", auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-operation_date", "-executed_at", "-pk")
        verbose_name = "Платёж из банка"
        verbose_name_plural = "Платежи из банка"

    def __str__(self):
        return f"{self.operation_date:%d.%m.%Y} · {self.amount} {self.currency} · {self.counterparty_name}"


class BankSyncState(models.Model):
    """Одна строка (pk=1): когда последний раз забирали платежи и чем закончилось."""

    last_synced_at = models.DateTimeField("Последняя синхронизация", null=True, blank=True)
    last_status = models.CharField("Результат", max_length=255, blank=True)

    class Meta:
        verbose_name = "Состояние синхронизации с банком"
        verbose_name_plural = "Состояние синхронизации с банком"

    def __str__(self):
        return self.last_status or "нет данных"

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]


class CashAuditLog(models.Model):
    ACTION_CREATED = "created"
    ACTION_UPDATED = "updated"
    ACTION_DELETED = "deleted"
    ACTION_RECONCILED = "reconciled"
    ACTION_CHOICES = (
        (ACTION_CREATED, "Создано"),
        (ACTION_UPDATED, "Изменено"),
        (ACTION_DELETED, "Удалено"),
        (ACTION_RECONCILED, "Сверка"),
    )

    transaction = models.ForeignKey(
        CashTransaction,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cash_audit_events",
    )
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    message = models.TextField()
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-occurred_at", "-pk")
        verbose_name = "История кассы"
        verbose_name_plural = "История кассы"

    def __str__(self):
        return self.message
