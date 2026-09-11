from decimal import Decimal

from django.db import models


class FilterSettings(models.Model):
    include_words = models.TextField(
        "Плюс-слова",
        blank=True,
        help_text="Через запятую или с новой строки. Тендер проходит, если в названии есть хотя бы одно. "
        "Знак + внутри — «все части сразу»: живых+цветов = оба слова. Пусто — берём все.",
    )
    exclude_words = models.TextField(
        "Минус-слова",
        blank=True,
        help_text="Через запятую или с новой строки. Если в названии есть хотя бы одно — тендер скрывается.",
    )
    min_price = models.DecimalField("Минимальная НМЦК, ₽", max_digits=16, decimal_places=2, default=Decimal("300000"))
    window_days = models.PositiveSmallIntegerField("Окно по дате публикации, дней", default=7)
    okpd2_codes = models.JSONField("Категории ОКПД2", default=list, blank=True)
    regions = models.JSONField("Регионы (коды)", default=list, blank=True)
    laws = models.JSONField("Источники", default=list, blank=True)

    class Meta:
        verbose_name = "Настройки подбора"
        verbose_name_plural = "Настройки подбора"

    def __str__(self):
        return "Настройки подбора"

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]


class FoundTender(models.Model):
    NEW = "new"
    DISMISSED = "dismissed"
    PUSHED = "pushed"
    STATUS_CHOICES = (
        (NEW, "Новый"),
        (DISMISSED, "Скрыт"),
        (PUSHED, "На расчёте"),
    )

    UNREVIEWED = "unreviewed"
    INTERESTING = "interesting"
    NOT_INTERESTING = "not_interesting"
    REVIEW_CHOICES = (
        (UNREVIEWED, "Не проверен"),
        (INTERESTING, "Интересно"),
        (NOT_INTERESTING, "Не интересно"),
    )

    LAW_CHOICES = (("fz44", "44-ФЗ"), ("fz223", "223-ФЗ"))

    law = models.CharField("Закон", max_length=8, choices=LAW_CHOICES, default="fz44", db_index=True)
    purchase_number = models.CharField("Номер закупки", max_length=40, db_index=True)
    object_info = models.TextField("Наименование объекта закупки (как в ЕИС)")
    title = models.TextField("Название", blank=True)
    max_price = models.DecimalField("НМЦК", max_digits=16, decimal_places=2, null=True, blank=True)
    currency_code = models.CharField("Валюта", max_length=8, blank=True)
    customer_inn = models.CharField("ИНН заказчика", max_length=32, blank=True)
    region = models.PositiveIntegerField("Код региона", null=True, blank=True)
    stage = models.PositiveSmallIntegerField("Этап", null=True, blank=True)
    purchase_type = models.CharField("Тип закупки", max_length=64, blank=True)
    okpd2 = models.JSONField("ОКПД2", default=list, blank=True)
    published_at = models.DateTimeField("Опубликовано", null=True, blank=True)
    collecting_finished_at = models.DateTimeField("Окончание подачи заявок", null=True, blank=True)
    eis_url = models.URLField("Ссылка на ЕИС", max_length=500, blank=True)
    raw = models.JSONField("Ответ API", default=dict, blank=True)
    notification_raw = models.JSONField("Извещение (сырое)", default=dict, blank=True)
    notification_checked_at = models.DateTimeField("Извещение загружено", null=True, blank=True)
    clarifications_raw = models.JSONField("Разъяснения (сырое)", default=list, blank=True)
    complaints_raw = models.JSONField("Жалобы (сырое)", default=list, blank=True)
    extras_checked_at = models.DateTimeField("Разъяснения/жалобы загружены", null=True, blank=True)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=NEW)
    review = models.CharField("Проверка", max_length=16, choices=REVIEW_CHOICES, default=UNREVIEWED)
    pushed_estimate_id = models.PositiveIntegerField("ID просчёта", null=True, blank=True)
    first_seen_at = models.DateTimeField("Впервые найден", auto_now_add=True)
    last_pulled_at = models.DateTimeField("Последняя выгрузка")

    class Meta:
        ordering = ["-published_at", "-first_seen_at"]
        constraints = [
            models.UniqueConstraint(fields=["law", "purchase_number"], name="uniq_law_purchase_number"),
        ]
        verbose_name = "Найденный тендер"
        verbose_name_plural = "Найденные тендеры"

    def __str__(self):
        return f"{self.purchase_number} — {self.title or self.object_info}"


class Organization(models.Model):
    """Кэш карточек заказчиков по ИНН — подтягивается из отдельного эндпоинта."""

    inn = models.CharField("ИНН", max_length=32, unique=True, db_index=True)
    name = models.CharField("Название", max_length=400, blank=True)
    short_name = models.CharField("Короткое название", max_length=200, blank=True)
    city = models.CharField("Город", max_length=200, blank=True)
    region_name = models.CharField("Регион", max_length=200, blank=True)
    address = models.CharField("Адрес", max_length=400, blank=True)
    email = models.CharField("Почта", max_length=200, blank=True)
    website = models.CharField("Сайт", max_length=200, blank=True)
    checked_at = models.DateTimeField("Проверено", auto_now=True)

    class Meta:
        verbose_name = "Заказчик"
        verbose_name_plural = "Заказчики"

    def __str__(self):
        return self.name or self.inn


class DocumentPreview(models.Model):
    """Кэш разобранного документа извещения (по прямой ссылке ЕИС)."""

    url = models.URLField("Ссылка", max_length=600, unique=True)
    filename = models.CharField("Имя файла", max_length=400, blank=True)
    kind = models.CharField("Тип", max_length=16, blank=True)
    html = models.TextField("Содержимое (HTML)", blank=True)
    error = models.CharField("Ошибка", max_length=400, blank=True)
    fetched_at = models.DateTimeField("Загружено", auto_now=True)

    class Meta:
        verbose_name = "Предпросмотр документа"
        verbose_name_plural = "Предпросмотры документов"

    def __str__(self):
        return self.filename or self.url


class ContractStat(models.Model):
    """Одна завершённая закупка: начальная цена, итог контракта, снижение.

    Наполняется фоновым сборщиком из реестра контрактов + карточек закупок.
    Используется для оценки предполагаемого снижения по похожим тендерам.
    """

    law = models.CharField("Закон", max_length=8, default="fz44", db_index=True)
    purchase_number = models.CharField("Номер закупки", max_length=40, db_index=True)
    contract_reg_num = models.CharField("Номер контракта", max_length=40, blank=True, db_index=True)
    shared_purchase = models.BooleanField("Совместная / многолотовая закупка", default=False, db_index=True)
    category = models.CharField("Категория (группа ОКПД2)", max_length=12, blank=True, db_index=True)
    okpd2 = models.JSONField("Коды ОКПД2", default=list, blank=True)
    ktru = models.JSONField("Коды КТРУ", default=list, blank=True)
    region = models.PositiveIntegerField("Код региона", null=True, blank=True, db_index=True)
    subject = models.TextField("Предмет контракта", blank=True)
    customer_inn = models.CharField("ИНН заказчика", max_length=32, blank=True, db_index=True)
    nmck = models.DecimalField("Начальная цена (НМЦК)", max_digits=16, decimal_places=2, null=True, blank=True)
    final_price = models.DecimalField("Итоговая цена контракта", max_digits=16, decimal_places=2, null=True, blank=True)
    discount_pct = models.DecimalField("Снижение, %", max_digits=5, decimal_places=1, null=True, blank=True, db_index=True)
    participants_count = models.PositiveSmallIntegerField("Участников", null=True, blank=True)
    winner_inn = models.CharField("ИНН победителя", max_length=32, blank=True)
    is_ours = models.BooleanField("Наш тендер", default=False)
    contract_date = models.DateField("Дата контракта", null=True, blank=True, db_index=True)
    nmck_checked = models.BooleanField("Начальная цена добрана", default=False, db_index=True)
    collected_at = models.DateTimeField("Собрано", auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["law", "contract_reg_num"], name="uniq_stat_law_reg"),
        ]
        ordering = ["-contract_date", "-collected_at"]
        verbose_name = "Статистика по контракту"
        verbose_name_plural = "Статистика по контрактам"

    def __str__(self):
        return f"{self.purchase_number} — {self.discount_pct or '?'}%"


class StatsRun(models.Model):
    started_at = models.DateTimeField("Начало")
    finished_at = models.DateTimeField("Конец", null=True, blank=True)
    params = models.JSONField("Параметры", default=dict, blank=True)
    requests_made = models.PositiveIntegerField("Запросов к API", default=0)
    contracts_seen = models.PositiveIntegerField("Контрактов просмотрено", default=0)
    created_count = models.PositiveIntegerField("Новых строк", default=0)
    filled_count = models.PositiveIntegerField("Добрано начальных цен", default=0)
    duration_seconds = models.FloatField("Длительность, сек", default=0.0)
    ok = models.BooleanField("Успех", default=False)
    error = models.TextField("Ошибка", blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Сбор статистики"
        verbose_name_plural = "Сборы статистики"

    def __str__(self):
        return f"{self.started_at:%d.%m.%Y %H:%M} — +{self.created_count}"


class PullRun(models.Model):
    started_at = models.DateTimeField("Начало")
    finished_at = models.DateTimeField("Конец", null=True, blank=True)
    params = models.JSONField("Параметры запроса", default=dict, blank=True)
    requests_made = models.PositiveIntegerField("Запросов к API", default=0)
    records_received = models.PositiveIntegerField("Получено записей", default=0)
    created_count = models.PositiveIntegerField("Новых", default=0)
    updated_count = models.PositiveIntegerField("Обновлено", default=0)
    duration_seconds = models.FloatField("Длительность, сек", default=0.0)
    ok = models.BooleanField("Успех", default=False)
    error = models.TextField("Ошибка", blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Выгрузка"
        verbose_name_plural = "Выгрузки"

    def __str__(self):
        return f"{self.started_at:%d.%m.%Y %H:%M} — {self.records_received} записей"
