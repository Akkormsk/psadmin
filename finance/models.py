from django.conf import settings
from django.db import models


class ManagerSettings(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    shift_rate = models.DecimalField("Ставка смены", max_digits=10, decimal_places=2, default=0)
    leave_shift_rate = models.DecimalField("Ставка отпускной смены", max_digits=10, decimal_places=2, default=0)

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class KpiTier(models.Model):
    threshold = models.DecimalField("План по прибыли от", max_digits=12, decimal_places=2, unique=True)

    class Meta:
        ordering = ("threshold",)

    def __str__(self):
        return f"От {self.threshold:,.0f} ₽"


class ManagerKpiRate(models.Model):
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    tier = models.ForeignKey(KpiTier, on_delete=models.CASCADE)
    percent = models.DecimalField("KPI, %", max_digits=5, decimal_places=2, default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("manager", "tier"), name="unique_manager_kpi_tier")]


class FinancialPeriod(models.Model):
    code = models.CharField("Учётный период", max_length=7, unique=True)
    is_closed = models.BooleanField("Закрыт", default=False)

    class Meta:
        ordering = ("-code",)

    def __str__(self):
        return self.code


class OperatingExpense(models.Model):
    name = models.CharField("Название расхода", max_length=120, unique=True)
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)
    is_active = models.BooleanField("Учитывать", default=True)

    class Meta:
        ordering = ("name",)
        verbose_name = "Постоянный расход"
        verbose_name_plural = "Постоянные расходы"

    def __str__(self):
        return self.name


class PeriodExpense(models.Model):
    period = models.ForeignKey(FinancialPeriod, on_delete=models.CASCADE, related_name="expenses")
    template = models.ForeignKey(OperatingExpense, null=True, blank=True, on_delete=models.SET_NULL)
    name = models.CharField("Название расхода", max_length=120)
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("period", "template"), name="unique_period_expense_template")]
        verbose_name = "Расход периода"
        verbose_name_plural = "Расходы периода"


class PayrollLine(models.Model):
    MANAGER = "manager"
    PRINTER = "printer"
    DESIGNER = "designer"
    KIND_CHOICES = ((MANAGER, "Менеджер"), (PRINTER, "Печатник"), (DESIGNER, "Дизайнер"))

    period = models.ForeignKey(FinancialPeriod, on_delete=models.CASCADE, related_name="payroll_lines")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    name = models.CharField("Сотрудник", max_length=120)
    work_shifts = models.PositiveIntegerField("Рабочие смены", default=0)
    leave_shifts = models.PositiveIntegerField("Отпускные/больничные смены", default=0)
    shift_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    leave_shift_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    fixed_salary = models.DecimalField("Фиксированный оклад", max_digits=12, decimal_places=2, default=0)
    design_amount = models.DecimalField("Стоимость макетов", max_digits=12, decimal_places=2, default=0)
    design_percent = models.DecimalField("Доля дизайнера, %", max_digits=5, decimal_places=2, default=90)
    deductions = models.DecimalField("Вычеты", max_digits=12, decimal_places=2, default=0)
    advance = models.DecimalField("Аванс", max_digits=12, decimal_places=2, default=0)
    order_profit = models.DecimalField("Прибыль по заказам", max_digits=12, decimal_places=2, default=0)
    kpi_percent = models.DecimalField("KPI, %", max_digits=5, decimal_places=2, default=0)
    kpi_bonus = models.DecimalField("Бонус KPI", max_digits=12, decimal_places=2, default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("period", "manager"), name="unique_manager_period")]
        verbose_name = "Строка зарплаты"
        verbose_name_plural = "Строки зарплаты"
