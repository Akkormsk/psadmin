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


class ProductionType(models.Model):
    code = models.SlugField("Код", max_length=80, unique=True)
    name = models.CharField("Тип производства", max_length=200)
    description = models.CharField("Краткие признаки", max_length=500, blank=True)
    sort_order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        ordering = ["sort_order", "pk"]
        verbose_name = "Тип производства"
        verbose_name_plural = "Типы производства"

    def __str__(self):
        return self.name


class ProductionTrainingExample(models.Model):
    production_type = models.ForeignKey(ProductionType, on_delete=models.PROTECT, related_name="examples", verbose_name="Подтверждённый тип")
    position_name = models.CharField("Наименование позиции", max_length=500)
    requirements = models.JSONField("Исходные требования", default=dict, blank=True)
    features = models.JSONField("Существенные признаки", default=list, blank=True)
    routes = models.JSONField("Подтверждённые маршруты", default=list, blank=True)
    note = models.CharField("Комментарий администратора", max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="production_training_examples")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Учебный пример производства"
        verbose_name_plural = "Учебные примеры производства"

    def __str__(self):
        return f"{self.position_name} → {self.production_type}"


class ProcessDefinition(models.Model):
    ROLE_SUPPLY = "supply"
    ROLE_PRODUCTION = "production"
    ROLE_COMPLETION = "completion"
    ROLE_CHOICES = [
        (ROLE_SUPPLY, "Снабжение"),
        (ROLE_PRODUCTION, "Производство"),
        (ROLE_COMPLETION, "Завершение и логистика"),
    ]

    name = models.CharField("Процесс", max_length=200)
    role = models.CharField("Роль", max_length=20, choices=ROLE_CHOICES)
    description = models.CharField("Когда применяется", max_length=500, blank=True)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        ordering = ["role", "name"]
        constraints = [models.UniqueConstraint(fields=["name", "role"], name="unique_tender_process_role")]
        verbose_name = "Процесс маршрута"
        verbose_name_plural = "Процессы маршрутов"

    def __str__(self):
        return self.name


class ProductionTrainingSession(models.Model):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="production_training_sessions")
    position_name = models.CharField("Наименование позиции", max_length=500)
    requirements = models.JSONField("Требования позиции", default=dict, blank=True)
    current_hypothesis = models.JSONField("Текущая гипотеза", default=dict, blank=True)
    is_confirmed = models.BooleanField("Подтверждена", default=False)
    confirmed_example = models.ForeignKey(ProductionTrainingExample, on_delete=models.SET_NULL, null=True, blank=True, related_name="training_sessions")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Сессия обучения расчёту"
        verbose_name_plural = "Сессии обучения расчётам"

    def __str__(self):
        return self.position_name


class ProductionTrainingTurn(models.Model):
    session = models.ForeignKey(ProductionTrainingSession, on_delete=models.CASCADE, related_name="turns")
    feedback = models.TextField("Комментарий администратора", blank=True)
    understood_changes = models.JSONField("Понятые изменения", default=list, blank=True)
    hypothesis = models.JSONField("Версия расчёта", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name = "Версия обучающего диалога"
        verbose_name_plural = "Версии обучающих диалогов"

    def __str__(self):
        return f"{self.session.position_name} · {self.pk}"


class TenderKnowledgeSource(models.Model):
    SOURCE_CHOICES = [
        ("link", "Ссылка"),
        ("document", "Документ"),
        ("image", "Изображение"),
        ("text", "Текст"),
        ("catalog", "Каталог / API"),
    ]

    title = models.CharField("Источник", max_length=300)
    supplier_name = models.CharField("Поставщик", max_length=200, blank=True)
    source_type = models.CharField("Тип", max_length=20, choices=SOURCE_CHOICES)
    url = models.URLField("Ссылка", max_length=1000, blank=True)
    content_summary = models.TextField("Извлечённые данные", blank=True)
    structured_data = models.JSONField("Структурированные данные", default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tender_knowledge_sources")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        ordering = ["supplier_name", "title", "-updated_at"]
        verbose_name = "Источник расчёта"
        verbose_name_plural = "Источники расчётов"

    def __str__(self):
        return f"{self.supplier_name + ' · ' if self.supplier_name else ''}{self.title}"


class TenderEstimate(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tender_estimates", verbose_name="Ответственный")
    tender_number = models.CharField("Номер тендера", max_length=100)
    name = models.CharField("Название / комментарий", max_length=300)
    reduction_percent = models.DecimalField("Снижение цены, %", max_digits=5, decimal_places=2, default=Decimal("30.00"))
    russia_delivery = models.DecimalField("Доставка по РФ", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    vat_rate_snapshot = models.DecimalField("НДС, %", max_digits=5, decimal_places=2, default=Decimal("5.00"))
    summary_snapshot = models.JSONField(default=dict, blank=True)
    document_analysis = models.JSONField("Анализ документов", default=dict, blank=True)
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
    requirements = models.JSONField("Требования из ООЗ/ТЗ", default=dict, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "pk"]
        verbose_name = "Товар"
        verbose_name_plural = "Товары"

    def __str__(self):
        return self.name
