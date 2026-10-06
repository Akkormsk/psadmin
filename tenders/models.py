from decimal import Decimal
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class TenderSettings(models.Model):
    vat_rate = models.DecimalField("НДС, %", max_digits=5, decimal_places=2, default=Decimal("5.00"))
    auto_start_product_search = models.BooleanField("Автозапуск подбора при открытии", default=False)
    auto_recalculate_requirements = models.BooleanField("Автопересчёт при изменении ТЗ", default=False)
    # Единственный источник порогов ROI — раскраска бейджей (tender_selection)
    # и экономический блок расчёта (verdict_for) читают ровно эти два числа,
    # без своих копий; правит админ здесь, без деплоя.
    roi_good_percent = models.DecimalField("ROI — зелёная зона, от %", max_digits=5, decimal_places=2, default=Decimal("30.00"))
    roi_thin_percent = models.DecimalField("ROI — жёлтая зона, от %", max_digits=5, decimal_places=2, default=Decimal("15.00"))
    default_reduction_percent = models.DecimalField(
        "Снижение по умолчанию для нового расчёта, %", max_digits=5, decimal_places=2, default=Decimal("30.00"),
    )

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
    knowledge_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    production_type = models.ForeignKey(ProductionType, on_delete=models.PROTECT, related_name="examples", verbose_name="Подтверждённый тип")
    position_name = models.CharField("Наименование позиции", max_length=500)
    requirements = models.JSONField("Исходные требования", default=dict, blank=True)
    features = models.JSONField("Существенные признаки", default=list, blank=True)
    routes = models.JSONField("Подтверждённые маршруты", default=list, blank=True)
    note = models.CharField("Комментарий администратора", max_length=500, blank=True)
    is_active = models.BooleanField("Используется в обучении", default=True)
    superseded_by = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="superseded_examples")
    embedding = models.JSONField("Смысловой индекс", default=list, blank=True)
    embedding_model = models.CharField("Модель смыслового индекса", max_length=100, blank=True)
    embedding_updated_at = models.DateTimeField("Индекс обновлён", null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="production_training_examples")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Учебный пример производства"
        verbose_name_plural = "Учебные примеры производства"

    def __str__(self):
        return f"{self.position_name} → {self.production_type}"


class ProcessDefinition(models.Model):
    """Технологический этап («База производства»).

    `role` — прежнее техническое поле: его по-прежнему читает код маршрута
    (`routes.py`) и лаборатория каскада при создании нового процесса из
    предложения ассистента. Пользователю в новом UI «Базы производства» роль
    не показывается — вместо неё видны `supplies_input`/`performs_production`/
    `terminal_mode`. Обе группы полей сосуществуют на переходный период,
    вторая не подменяет первую в коде, который её уже использует."""

    ROLE_SUPPLY = "supply"
    ROLE_PRODUCTION = "production"
    ROLE_COMPLETION = "completion"
    ROLE_CHOICES = [
        (ROLE_SUPPLY, "Снабжение"),
        (ROLE_PRODUCTION, "Производство"),
        (ROLE_COMPLETION, "Завершение и логистика"),
    ]

    TERMINAL_ALWAYS = "always"
    TERMINAL_NEVER = "never"
    TERMINAL_SOMETIMES = "sometimes"
    TERMINAL_CHOICES = [
        (TERMINAL_ALWAYS, "Всегда"),
        (TERMINAL_NEVER, "Никогда"),
        (TERMINAL_SOMETIMES, "Иногда"),
    ]

    name = models.CharField("Процесс", max_length=200)
    role = models.CharField("Роль", max_length=20, choices=ROLE_CHOICES)
    description = models.CharField("Когда применяется", max_length=500, blank=True)
    is_active = models.BooleanField("Активен", default=True)

    supplies_input = models.BooleanField("Предоставляет изделие/материал", default=False)
    performs_production = models.BooleanField("Выполняет производство", default=False)
    terminal_mode = models.CharField("Завершает маршрут", max_length=10, choices=TERMINAL_CHOICES, default=TERMINAL_SOMETIMES)
    scope_tags = models.JSONField("Что производим", default=list, blank=True)
    when_to_use = models.TextField("Когда использовать", blank=True)
    when_not_to_use = models.TextField("Когда не использовать", blank=True)
    parameters = models.JSONField("Параметры {required:[], optional:[]}", default=dict, blank=True)

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


class RequirementSkipRule(models.Model):
    """A ТЗ characteristic whose row the admin marked "не участвует в
    подборе" — a labelling/compliance/design detail, not a real product
    criterion ("Маркировка Честного Знака", "макет в трёх вариантах",
    "ярлык с составом"). "Принять и обучить" promotes the unchecked rows
    here by their normalised label; every later tender pre-unchecks a row
    of that label, so the same paperwork is never re-evaluated by hand."""
    label = models.CharField("Название строки ТЗ", max_length=200)
    label_normalized = models.CharField("Название в нормальной форме", max_length=200, unique=True)
    example_value = models.CharField("Пример значения", max_length=500, blank=True)
    is_active = models.BooleanField("Активно", default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="requirement_skip_rules")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Строка ТЗ вне подбора"
        verbose_name_plural = "Строки ТЗ вне подбора"

    def __str__(self):
        return self.label


class Lesson(models.Model):
    """One thing the admin taught the assistant about a search, kept as the
    admin's own words plus the AI's short restatement, tagged with the
    context it was learned in — what kind of item, which ТЗ field labels
    were present, which production step. "Принять и обучить" writes one row
    per feedback phrase; every later search on a similar position feeds
    every matching lesson back into the single AI pass over the shortlist,
    so a pattern that repeats even once is already active. No weights, no
    scores: the AI reads all matching lessons as plain text and decides by
    the current context. This one table replaced the older trigger→action
    search-rule DSL; the RequirementSkipRule checkboxes stay separate for
    now."""
    SCOPE_CHOICES = [
        ("catalog", "Подбор товара"),
        ("requirements", "Строки ТЗ"),
        ("route", "Маршрут"),
        ("production_step", "Этап производства"),
        ("cost", "Себестоимость"),
    ]
    SOURCE_MANUAL = "manual"
    SOURCE_FEEDBACK = "feedback"
    SOURCE_PROPOSAL = "proposal"
    SOURCE_IMPORT = "import"
    SOURCE_PRESET = "preset"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Вручную"),
        (SOURCE_FEEDBACK, "Из фидбэка"),
        (SOURCE_PROPOSAL, "Подтверждённое предложение"),
        (SOURCE_IMPORT, "Импорт"),
        (SOURCE_PRESET, "Стартовый набор"),
    ]

    scope = models.CharField("Область", max_length=32, choices=SCOPE_CHOICES, default="catalog")
    admin_text = models.TextField("Слова администратора")
    summary = models.CharField("Чистая формулировка от ИИ", max_length=300, blank=True)
    item_word = models.CharField("Слово-товар", max_length=120, blank=True)
    tz_labels = models.JSONField("Метки полей ТЗ", default=list, blank=True)
    production_type = models.CharField("Тип производства", max_length=120, blank=True)
    outcome = models.JSONField("Что вышло в прошлый раз", default=dict, blank=True)
    source = models.CharField("Источник знания", max_length=16, choices=SOURCE_CHOICES, default=SOURCE_FEEDBACK)
    is_active = models.BooleanField("Активно", default=True)
    session = models.ForeignKey(ProductionTrainingSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="lessons")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assistant_lessons")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["scope", "is_active"])]
        verbose_name = "Урок ассистента"
        verbose_name_plural = "Уроки ассистента"

    def __str__(self):
        return self.summary or self.admin_text[:80]


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

    knowledge_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField("Источник", max_length=300)
    supplier_name = models.CharField("Поставщик", max_length=200, blank=True)
    source_type = models.CharField("Тип", max_length=20, choices=SOURCE_CHOICES)
    url = models.URLField("Ссылка", max_length=1000, blank=True)
    content_summary = models.TextField("Извлечённые данные", blank=True)
    structured_data = models.JSONField("Структурированные данные", default=dict, blank=True)
    counterparty = models.ForeignKey("Counterparty", on_delete=models.SET_NULL, null=True, blank=True, related_name="knowledge_sources")
    # Сам файл (скриншот/переписка) хранится в базе, не на диске контейнера —
    # диск не переживает пересборку образа. Лимит размера (20 МБ) проверяет
    # вью при загрузке, не сама модель.
    raw_file = models.BinaryField("Файл источника", null=True, blank=True)
    raw_file_name = models.CharField("Имя файла", max_length=255, blank=True)
    raw_file_content_type = models.CharField("MIME-тип файла", max_length=100, blank=True)
    superseded_by = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="superseded_sources")
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


class UnitAlias(models.Model):
    """Один вариант написания физической единицы измерения → канонический
    вид (см. tenders/cascade.py, _canonical_unit/_units_compatible). Ручное
    сравнение единиц как текста ловит не все написания одной и той же
    величины («г/м2» vs «г/м²»), а хардкод в коде требовал бы деплоя на
    каждое новое написание — таблица растёт сама по мере встречающихся
    случаев, без деплоя: новую строку добавляет администратор через админку
    (или management-команда), а код подхватывает её сразу же (кэш в памяти
    сбрасывается по сигналу при любом изменении таблицы, см. cascade.py)."""
    spelling = models.CharField("Написание", max_length=40, unique=True)
    canonical = models.CharField("Каноническая единица", max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["canonical", "spelling"]
        verbose_name = "Единица измерения — написание"
        verbose_name_plural = "Единицы измерения — написания"

    def __str__(self):
        return f"{self.spelling} → {self.canonical}"


class AttributeConceptHint(models.Model):
    """Самообучающийся словарь: какое название атрибута карточки отвечает на
    какое понятие критерия ТЗ (см. tenders/cascade.py, _learned_attribute_name/
    _remember_attribute_hint). Ни одно понятие сюда не зашито заранее — строку
    добавляет код сам, только когда агент шага 6 явно подтвердил соответствие
    на реальной карточке; при повторном подтверждении растёт счётчик, а не
    новая строка. Одно и то же понятие («ёмкость памяти», «объём тары»,
    «плотность ткани» — что угодно) у разных поставщиков может отвечать
    по-разному — таблица держит все варианты сразу и решает по частоте."""

    concept_key = models.CharField("Ключ понятия", max_length=200, db_index=True)
    attribute_name = models.CharField("Название атрибута", max_length=120)
    hits = models.PositiveIntegerField("Подтверждений", default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-hits", "-updated_at"]
        constraints = [
            models.UniqueConstraint(fields=["concept_key", "attribute_name"], name="unique_concept_attribute_hint"),
        ]
        verbose_name = "Подсказка атрибута (словарь)"
        verbose_name_plural = "Подсказки атрибутов (словарь)"

    def __str__(self):
        return f"{self.concept_key} → {self.attribute_name} ({self.hits})"


class CatalogSupplier(models.Model):
    code = models.SlugField("Код", max_length=50, unique=True)
    name = models.CharField("Поставщик", max_length=200)
    base_url = models.URLField("Адрес API", max_length=500)
    is_active = models.BooleanField("Активен", default=True)
    last_synced_at = models.DateTimeField("Последняя синхронизация", null=True, blank=True)
    sync_status = models.CharField("Состояние", max_length=20, default="never")
    sync_message = models.CharField("Сообщение", max_length=500, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Поставщик каталога"
        verbose_name_plural = "Поставщики каталогов"

    def __str__(self):
        return self.name


class CatalogCategory(models.Model):
    supplier = models.ForeignKey(CatalogSupplier, on_delete=models.CASCADE, related_name="categories")
    external_id = models.CharField("ID поставщика", max_length=100)
    parent_external_id = models.CharField("Родительский ID", max_length=100, blank=True)
    name = models.CharField("Категория", max_length=300)
    path = models.CharField("Полный путь", max_length=1000, blank=True)
    embedding = models.JSONField("Смысловой индекс", default=list, blank=True)
    embedding_model = models.CharField("Модель смыслового индекса", max_length=100, blank=True)
    embedding_text_hash = models.CharField("Хеш смыслового представления", max_length=64, blank=True)
    embedding_updated_at = models.DateTimeField("Индекс обновлён", null=True, blank=True)
    is_active = models.BooleanField("Активна", default=True)

    class Meta:
        ordering = ["supplier", "path", "name"]
        constraints = [models.UniqueConstraint(fields=["supplier", "external_id"], name="unique_catalog_category_supplier_id")]
        indexes = [models.Index(fields=["supplier", "is_active"])]
        verbose_name = "Категория каталога"
        verbose_name_plural = "Категории каталога"

    def __str__(self):
        return self.path or self.name


class CatalogProduct(models.Model):
    supplier = models.ForeignKey(CatalogSupplier, on_delete=models.CASCADE, related_name="products")
    external_id = models.CharField("ID поставщика", max_length=100)
    article = models.CharField("Артикул", max_length=120, blank=True)
    article_base = models.CharField("Базовый артикул", max_length=120, blank=True)
    group_id = models.CharField("Группа товара", max_length=120, blank=True)
    color_group_id = models.CharField("Группа цвета", max_length=120, blank=True)
    family_key = models.CharField("Нормализованное семейство", max_length=180, blank=True, db_index=True)
    name = models.CharField("Название", max_length=500)
    full_name = models.CharField("Полное название", max_length=1000, blank=True)
    description = models.TextField("Описание", blank=True)
    category_ids = models.JSONField("Категории", default=list, blank=True)
    category_names = models.JSONField("Названия категорий", default=list, blank=True)
    brand = models.CharField("Бренд", max_length=200, blank=True)
    size = models.CharField("Размер", max_length=100, blank=True)
    materials = models.JSONField("Материалы", default=list, blank=True)
    colors = models.JSONField("Цвета", default=list, blank=True)
    attributes = models.JSONField("Характеристики", default=list, blank=True)
    branding = models.JSONField("Виды нанесения", default=list, blank=True)
    package = models.JSONField("Упаковка", default=list, blank=True)
    price = models.DecimalField("Цена", max_digits=14, decimal_places=2, null=True, blank=True)
    discount_price = models.DecimalField("Дилерская цена", max_digits=14, decimal_places=2, null=True, blank=True)
    total_stock = models.IntegerField("Свободный остаток", default=0)
    stock_moscow = models.IntegerField("Москва", default=0)
    stock_remote = models.IntegerField("Удалённый склад", default=0)
    stock_transit = models.IntegerField("В пути", default=0)
    is_on_order = models.BooleanField("Под заказ", default=False)
    delivery_days = models.PositiveIntegerField("Дней до поставки", null=True, blank=True)
    image_url = models.URLField("Изображение", max_length=1000, blank=True)
    product_url = models.URLField("Карточка товара", max_length=1000, blank=True)
    supply_terms = models.CharField("Условия поставки", max_length=1000, blank=True)
    warning = models.CharField("Важное примечание", max_length=1000, blank=True)
    defect = models.CharField("Дефекты", max_length=1000, blank=True)
    search_text = models.TextField("Поисковый индекс", blank=True)
    source_updated_at = models.DateTimeField("Обновлено поставщиком", null=True, blank=True)
    synced_at = models.DateTimeField("Получено", auto_now=True)
    sync_marker = models.CharField(max_length=36, blank=True, db_index=True, editable=False)
    is_active = models.BooleanField("Активен", default=True)
    raw_data = models.JSONField("Служебные данные", default=dict, blank=True)

    class Meta:
        ordering = ["supplier", "name", "article"]
        constraints = [models.UniqueConstraint(fields=["supplier", "external_id"], name="unique_catalog_product_supplier_id")]
        indexes = [
            models.Index(fields=["supplier", "is_active"]),
            models.Index(fields=["supplier", "article"]),
            models.Index(fields=["supplier", "group_id"]),
            models.Index(fields=["supplier", "total_stock"]),
        ]
        verbose_name = "Товар каталога"
        verbose_name_plural = "Товары каталога"

    @property
    def effective_price(self):
        return self.discount_price if self.discount_price is not None else self.price

    def __str__(self):
        return f"{self.article} · {self.full_name or self.name}" if self.article else (self.full_name or self.name)


class CascadeCache(models.Model):
    """Кэш каскада подбора товара (tenders/cascade.py).

    - kind="criteria": ключ = версия + SHA1 названия позиции и строк ТЗ + модель.
      payload = {criteria}. Шаг 1 не создаёт поисковый план.
    - kind="searchplan": ключ = версия + модель + исходное название.
      payload = {item, queries}. Шаг 2 независимо чистит название и создаёт
      поисковые фразы; старый смешанный кэш шага 1 к нему не подходит.
    - kind="verdict": ключ = "<хэш ТЗ>|<id карточки>".
      payload = {"grid": {"1": ["y", ""], "2": ["n", "8 ГБ"]}}. На повторном
      прогоне того же ТЗ к агенту идут только карточки без записи.

    Записи не инвалидируются вручную — ключ несёт в себе всё. Старьё чистится
    командой prune_cascade_cache.
    """

    kind = models.CharField("Тип", max_length=16)
    key = models.CharField("Ключ", max_length=200)
    payload = models.JSONField("Содержимое", default=dict, blank=True)
    created_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "key"], name="unique_cascade_cache_entry")]
        indexes = [models.Index(fields=["kind", "key"])]
        verbose_name = "Кэш каскада"
        verbose_name_plural = "Кэш каскада"

    def __str__(self):
        return f"{self.kind}:{self.key[:40]}"


class CatalogSyncRun(models.Model):
    STATUS_CHOICES = [("running", "Выполняется"), ("success", "Готово"), ("failed", "Ошибка")]

    supplier = models.ForeignKey(CatalogSupplier, on_delete=models.CASCADE, related_name="sync_runs")
    status = models.CharField("Статус", max_length=20, choices=STATUS_CHOICES, default="running")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    received_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    deactivated_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Синхронизация каталога"
        verbose_name_plural = "Синхронизации каталога"

    def __str__(self):
        return f"{self.supplier} · {self.started_at:%d.%m.%Y %H:%M} · {self.status}"


class CatalogMatchDecision(models.Model):
    DECISION_CHOICES = [("selected", "Выбран"), ("rejected", "Отклонён")]

    session = models.ForeignKey(ProductionTrainingSession, on_delete=models.CASCADE, related_name="catalog_decisions")
    product = models.ForeignKey(CatalogProduct, on_delete=models.SET_NULL, null=True, blank=True, related_name="training_decisions")
    supplier_code = models.CharField("Код поставщика", max_length=50, default="oasis")
    product_external_id = models.CharField("ID товара поставщика", max_length=100, blank=True)
    product_article = models.CharField("Артикул", max_length=120, blank=True)
    product_snapshot = models.JSONField("Карточка товара на момент решения", default=dict, blank=True)
    decision = models.CharField("Решение", max_length=20, choices=DECISION_CHOICES)
    reason_codes = models.JSONField("Причины", default=list, blank=True)
    requirement_signature = models.JSONField("Требования на момент решения", default=dict, blank=True)
    note = models.CharField("Комментарий", max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="catalog_match_decisions")
    is_confirmed = models.BooleanField("Учитывать в обучении", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["decision", "is_confirmed"])]
        verbose_name = "Решение по товару каталога"
        verbose_name_plural = "Решения по товарам каталога"

    def __str__(self):
        product = self.product or self.product_article or self.product_external_id or "товар"
        return f"{self.get_decision_display()}: {product}"


class TenderEstimate(models.Model):
    """Расчёт цены для Tender — просто экономика: строки, свод, наша цена.
    Стадия сделки (черновик/на торгах/выигран/проигран), протокол и итог
    контракта — на Tender (estimate.tender), не здесь: расчёт можно
    пересчитать или удалить, а факт того, что случилось с тендером, должен
    остаться."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tender_estimates", verbose_name="Ответственный")
    tender = models.ForeignKey(
        "tender_selection.Tender", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="estimates", verbose_name="Тендер",
    )
    tender_number = models.CharField("Номер тендера", max_length=100)
    name = models.CharField("Название / комментарий", max_length=300)
    result_notes = models.TextField("Комментарий", blank=True)
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

    def display_status(self):
        """Черновик/Готово — не хранимый статус, а вид по полноте ЭТОГО расчёта
        (тендер может иметь несколько просчётов); дальше стадия сделки — общая
        для тендера, берём с него."""
        from tender_selection.models import Tender

        if self.tender_id and self.tender.outcome_status != Tender.OUTCOME_DRAFT:
            return self.tender.get_outcome_status_display()
        return "Готово" if not self.summary_snapshot.get("is_incomplete", True) else "Черновик"

    def is_active_task(self):
        """Расчёт ждёт действия менеджера, а не исхода: черновик и ещё не заполнен."""
        from tender_selection.models import Tender

        incomplete = self.summary_snapshot.get("is_incomplete", True)
        if not self.tender_id:
            return incomplete
        return self.tender.outcome_status == Tender.OUTCOME_DRAFT and incomplete


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


class Order(models.Model):
    """Заказ вне тендерного пайплайна — тот же жизненный цикл, что у Tender
    (стадия сделки живёт здесь, не на расчёте), но без ЕИС-машинерии: заказ
    создаётся вручную, а не приходит извне."""

    DRAFT = "draft"
    PENDING = "pending"
    NOT_PARTICIPATED = "not_participated"
    LOST = "lost"
    WON = "won"
    # Тот же набор, что у Tender.OUTCOME_STATUS_CHOICES, без "Итог опубликован" —
    # протоколов ЕИС у заказов нет. Не импортируем Tender (другое приложение,
    # цикл импорта) — набор простой, дублировать безопаснее.
    STATUS_CHOICES = (
        (DRAFT, "Черновик"),
        (PENDING, "На торгах"),
        (NOT_PARTICIPATED, "Не участвовали"),
        (LOST, "Проигран"),
        (WON, "Выигран"),
    )

    name = models.CharField("Название", max_length=300)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=DRAFT)
    archived_at = models.DateTimeField("В архиве с", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Заказ"
        verbose_name_plural = "Заказы"

    def __str__(self):
        return self.name

    def is_active_task(self):
        return self.status == self.DRAFT


class OrderEstimate(models.Model):
    """Расчёт цены для Order — пусть просто экономика: строки, свод, наша
    цена. Стадия сделки (черновик/на торгах/выигран/проигран) — на Order,
    не здесь, ровно как у TenderEstimate она теперь на Tender."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="order_estimates", verbose_name="Ответственный")
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="estimates", verbose_name="Заказ", null=True, blank=True)
    legacy_calculation_id = models.PositiveBigIntegerField(
        "Старый ID расчёта", null=True, blank=True, unique=True,
    )
    order_number = models.CharField("Номер расчёта", max_length=100)
    name = models.CharField("Название / комментарий", max_length=300)
    reduction_percent = models.DecimalField("Снижение цены, %", max_digits=5, decimal_places=2, default=Decimal("30.00"))
    russia_delivery = models.DecimalField("Доставка по РФ", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    vat_rate_snapshot = models.DecimalField("НДС, %", max_digits=5, decimal_places=2, default=Decimal("5.00"))
    summary_snapshot = models.JSONField(default=dict, blank=True)
    document_analysis = models.JSONField("Анализ документов", default=dict, blank=True)
    notes = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Расчёт заказа"
        verbose_name_plural = "Расчёты заказов"

    def __str__(self):
        return f"{self.order_number} — {self.name}"

    @property
    def tender_number(self):
        """Compatibility name for the shared calculation form.

        The field is deliberately stored as ``order_number``: an order
        calculation is not a tender and must not acquire a Tender relation.
        """
        return self.order_number

    @tender_number.setter
    def tender_number(self, value):
        self.order_number = value

    @property
    def result_notes(self):
        return self.notes

    @result_notes.setter
    def result_notes(self, value):
        self.notes = value

    def is_active_task(self):
        """Расчёт ждёт действия менеджера, а не исхода: черновик и ещё не заполнен."""
        incomplete = self.summary_snapshot.get("is_incomplete", True)
        if not self.order_id:
            return incomplete
        return self.order.status == Order.DRAFT and incomplete

    def display_status(self):
        """Делегирует стадию сделки заказу — своего статуса у расчёта нет."""
        if self.order_id:
            return self.order.get_status_display()
        return "Готово" if not self.summary_snapshot.get("is_incomplete", True) else "Черновик"


class OrderLine(models.Model):
    estimate = models.ForeignKey(OrderEstimate, on_delete=models.CASCADE, related_name="lines")
    name = models.CharField("Наименование", max_length=500)
    quantity = models.DecimalField("Количество", max_digits=14, decimal_places=2)
    nmck_unit = models.DecimalField("Цена за единицу", max_digits=14, decimal_places=2)
    material_unit = models.DecimalField("Материал", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    application_unit = models.DecimalField("Нанесение", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    logistics_unit = models.DecimalField("Логистика", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    product_url = models.URLField("Ссылка", max_length=1000, blank=True)
    comment = models.CharField("Комментарий", max_length=500, blank=True)
    requirements = models.JSONField("Требования", default=dict, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "pk"]
        verbose_name = "Позиция расчёта заказа"
        verbose_name_plural = "Позиции расчётов заказов"

    def __str__(self):
        return self.name


class CascadeLabPreset(models.Model):
    """Явно сохранённая администратором комбинация параметров каскада."""

    name = models.CharField("Название набора", max_length=200)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="cascade_lab_presets")
    settings = models.JSONField("Настройки шагов", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [models.UniqueConstraint(fields=["created_by", "name"], name="unique_cascade_lab_preset_name")]
        verbose_name = "Набор настроек каскада"
        verbose_name_plural = "Наборы настроек каскада"

    def __str__(self):
        return self.name


class CascadeConfigVersion(models.Model):
    """Версия настроек, используемая основным подбором товаров."""

    name = models.CharField("Название", max_length=200)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cascade_config_versions")
    settings = models.JSONField("Настройки", default=dict, blank=True)
    is_active = models.BooleanField("Активна", default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Версия настроек каскада"
        verbose_name_plural = "Версии настроек каскада"

    def __str__(self):
        return f"{self.name}{' · активна' if self.is_active else ''}"


class Counterparty(models.Model):
    """Контрагент «Базы производства» — тот, кто способен выполнить этап
    и как получить у него цену. Не имеет собственной роли: она целиком
    определяется его связями со Stage через StageCounterpartyLink. Может
    (необязательно) ссылаться на уже существующий автосинк-каталог
    (Oasis/Gifts) — это тот же контрагент, просто цену подаёт через API,
    а не скриншотом."""

    name = models.CharField("Название", max_length=200)
    catalog_supplier = models.ForeignKey(CatalogSupplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="counterparties")
    notes = models.TextField("Комментарий", blank=True)
    is_active = models.BooleanField("Активен", default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="counterparties")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Контрагент"
        verbose_name_plural = "Контрагенты"

    def __str__(self):
        return self.name


class StageCounterpartyLink(models.Model):
    """Связь этап↔контрагент — своя настройка на каждой паре. Несколько
    товарных шаблонов одного контрагента (как у FSPrint пакет/папка/каталог)
    живут внутри `settings`, отдельной таблицей не заводятся, пока не
    появится их собственный адаптер."""

    SOURCE_INTERNAL_CALCULATOR = "internal_calculator"
    SOURCE_CATALOG_API = "catalog_api"
    SOURCE_PRICE_LIST = "price_list"
    SOURCE_EXTERNAL_CALCULATOR = "external_calculator"
    SOURCE_EXTERNAL_API = "external_api"
    SOURCE_MANUAL_QUOTE = "manual_quote"
    SOURCE_HISTORICAL = "historical"
    PRICE_SOURCE_CHOICES = [
        (SOURCE_INTERNAL_CALCULATOR, "Внутренний калькулятор"),
        (SOURCE_CATALOG_API, "Каталог / API"),
        (SOURCE_PRICE_LIST, "Прайс-лист"),
        (SOURCE_EXTERNAL_CALCULATOR, "Внешний калькулятор"),
        (SOURCE_EXTERNAL_API, "Внешний API"),
        (SOURCE_MANUAL_QUOTE, "Ручной запрос цены"),
        (SOURCE_HISTORICAL, "История цен"),
    ]

    stage = models.ForeignKey(ProcessDefinition, on_delete=models.CASCADE, related_name="counterparty_links")
    counterparty = models.ForeignKey(Counterparty, on_delete=models.CASCADE, related_name="stage_links")
    is_active = models.BooleanField("Активна", default=True)
    priority = models.PositiveIntegerField("Приоритет (меньше — важнее)", default=0)
    price_source_type = models.CharField("Способ получения цены", max_length=24, choices=PRICE_SOURCE_CHOICES, default=SOURCE_MANUAL_QUOTE)
    settings = models.JSONField(
        "Настройки (ограничения, параметры, сроки, минимальный заказ, ссылка на калькулятор…)",
        default=dict, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "counterparty__name"]
        constraints = [models.UniqueConstraint(fields=["stage", "counterparty"], name="unique_stage_counterparty_link")]
        verbose_name = "Связь этап ↔ контрагент"
        verbose_name_plural = "Связи этап ↔ контрагент"

    def __str__(self):
        return f"{self.stage.name} ↔ {self.counterparty.name}"


class Proposal(models.Model):
    """Предложение глобального изменения «Базы производства». Ничего не
    меняет само по себе — только после `status=accepted` бэкенд выполняет
    обычную детерминированную операцию (создать/обновить Stage, создать
    Counterparty, создать/обновить связь, создать Lesson). Ручные правки
    внутри «Базы производства» тоже проходят через эту таблицу, сразу со
    статусом `accepted`, — так вся история глобальных изменений идёт
    одним путём и `Proposal` заодно служит её журналом."""

    TYPE_CREATE_STAGE = "create_stage"
    TYPE_UPDATE_STAGE = "update_stage"
    TYPE_CREATE_COUNTERPARTY = "create_counterparty"
    TYPE_UPDATE_COUNTERPARTY = "update_counterparty"
    TYPE_LINK_STAGE_COUNTERPARTY = "link_stage_counterparty"
    TYPE_CREATE_LESSON = "create_lesson"
    TYPE_CHOICES = [
        (TYPE_CREATE_STAGE, "Новый этап"),
        (TYPE_UPDATE_STAGE, "Изменить этап"),
        (TYPE_CREATE_COUNTERPARTY, "Новый контрагент"),
        (TYPE_UPDATE_COUNTERPARTY, "Изменить контрагента"),
        (TYPE_LINK_STAGE_COUNTERPARTY, "Связать этап и контрагента"),
        (TYPE_CREATE_LESSON, "Сохранить урок"),
    ]

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Ожидает подтверждения"),
        (STATUS_ACCEPTED, "Принято"),
        (STATUS_REJECTED, "Отклонено"),
    ]

    batch_id = models.UUIDField("Группа предложений", default=uuid.uuid4)
    type = models.CharField("Тип", max_length=32, choices=TYPE_CHOICES)
    payload = models.JSONField("Предлагаемая правка", default=dict, blank=True)
    summary = models.CharField("Формулировка для карточки", max_length=300)
    status = models.CharField("Статус", max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    session = models.ForeignKey(ProductionTrainingSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="proposals")
    source_text = models.TextField("Исходный фидбэк администратора", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_proposals")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="decided_proposals")
    decided_at = models.DateTimeField("Когда решили", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "batch_id"])]
        verbose_name = "Предложение изменения базы производства"
        verbose_name_plural = "Предложения изменения базы производства"

    def __str__(self):
        return self.summary
class Step4DecisionCache(models.Model):
    class Decision(models.TextChoices):
        PASS = "pass", "Подходит"
        REJECT = "reject", "Не подходит"

    target_signature = models.CharField("Нормализованный запрос", max_length=500)
    supplier = models.ForeignKey(CatalogSupplier, on_delete=models.CASCADE, related_name="step4_decision_cache_entries")
    product_external_id = models.CharField("ID товара поставщика", max_length=100)
    candidate_signature = models.CharField("Хеш названия кандидата", max_length=64)
    decision = models.CharField("Решение", max_length=8, choices=Decision.choices)
    model_name = models.CharField("Модель", max_length=120)
    contract_version = models.CharField("Версия контракта", max_length=64)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)
    last_used_at = models.DateTimeField("Последнее использование", null=True, blank=True)
    last_verified_at = models.DateTimeField("Последняя проверка", default=timezone.now)
    hit_count = models.PositiveIntegerField("Попаданий в кэш", default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["target_signature", "supplier", "product_external_id", "candidate_signature", "contract_version"],
                name="unique_step4_decision_cache_identity",
            ),
        ]
        verbose_name = "Решение кэша шага 4"
        verbose_name_plural = "Решения кэша шага 4"

    def __str__(self):
        return f"{self.supplier}:{self.product_external_id}:{self.decision}"

class TenderSourceItem(models.Model):
    tender = models.ForeignKey("tender_selection.Tender", on_delete=models.CASCADE, related_name="v2_source_items")
    source_key = models.CharField(max_length=160)
    source_type = models.CharField(max_length=32, default="notification")
    original_text = models.TextField()
    quantity = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=64, blank=True)
    requirements = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="derived_items")
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by_items")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=["tender","source_key"],name="unique_v2_source_item")]

class TenderComputeJob(models.Model):
    class Status(models.TextChoices):
        QUEUED="queued"; PREPARING_INPUT="preparing_input"; ROUTING="routing"; PREPARING="preparing"; PARTIAL="partial"; READY="ready"; NEEDS_REVIEW="needs_review"; FAILED="failed"; CANCELLED="cancelled"; RUNNING="running"
    tender=models.ForeignKey("tender_selection.Tender",on_delete=models.CASCADE,related_name="v2_compute_jobs")
    status=models.CharField(max_length=24,choices=Status.choices,default=Status.QUEUED)
    version=models.CharField(max_length=64,default="v2")
    trigger=models.CharField(max_length=64,default="manual")
    attempt_count=models.PositiveIntegerField(default=0)
    started_at=models.DateTimeField(null=True,blank=True); completed_at=models.DateTimeField(null=True,blank=True)
    diagnostics=models.JSONField(default=dict,blank=True); total_cost=models.DecimalField(max_digits=12,decimal_places=4,default=0); error=models.JSONField(default=dict,blank=True)
    created_at=models.DateTimeField(auto_now_add=True); updated_at=models.DateTimeField(auto_now=True)

class TenderComputeLine(models.Model):
    job=models.ForeignKey(TenderComputeJob,on_delete=models.CASCADE,related_name="lines")
    source_item=models.ForeignKey(TenderSourceItem,on_delete=models.PROTECT,related_name="compute_lines")
    input_snapshot=models.JSONField(default=dict,blank=True); route_key=models.CharField(max_length=100,blank=True); engine_key=models.CharField(max_length=100,blank=True); route_confidence=models.DecimalField(max_digits=5,decimal_places=4,null=True,blank=True); route_metadata=models.JSONField(default=dict,blank=True)
    status=models.CharField(max_length=32,default="queued"); result=models.JSONField(default=dict,blank=True); diagnostics=models.JSONField(default=dict,blank=True)
    class Meta: constraints=[models.UniqueConstraint(fields=["job","source_item"],name="unique_v2_compute_line")]

class TenderComputeWorkUnit(models.Model):
    job=models.ForeignKey(TenderComputeJob,on_delete=models.CASCADE,related_name="work_units")
    engine_key=models.CharField(max_length=100); dedupe_key=models.CharField(max_length=128); input_fingerprint=models.CharField(max_length=128); status=models.CharField(max_length=32,default="queued")
    lines=models.ManyToManyField(TenderComputeLine,related_name="work_units"); result=models.JSONField(default=dict,blank=True); diagnostics=models.JSONField(default=dict,blank=True); attempt_count=models.PositiveIntegerField(default=0); error=models.JSONField(default=dict,blank=True)
    class Meta: constraints=[models.UniqueConstraint(fields=["job","engine_key","dedupe_key"],name="unique_v2_work_unit")]

class TenderComputePreparation(models.Model):
    work_unit=models.ForeignKey(TenderComputeWorkUnit,on_delete=models.CASCADE,related_name="preparations"); engine_key=models.CharField(max_length=100); preparation_key=models.CharField(max_length=100); status=models.CharField(max_length=32,default="queued"); payload=models.JSONField(default=dict,blank=True); freshness=models.CharField(max_length=64,blank=True); cost=models.DecimalField(max_digits=12,decimal_places=4,default=0); attempt_count=models.PositiveIntegerField(default=0); started_at=models.DateTimeField(null=True,blank=True); completed_at=models.DateTimeField(null=True,blank=True); error=models.JSONField(default=dict,blank=True)
    class Meta: constraints=[models.UniqueConstraint(fields=["work_unit","engine_key","preparation_key"],name="unique_v2_preparation")]

class OwnerInteraction(models.Model):
    tender=models.ForeignKey("tender_selection.Tender",null=True,blank=True,on_delete=models.SET_NULL); source_item=models.ForeignKey(TenderSourceItem,null=True,blank=True,on_delete=models.SET_NULL); compute_line=models.ForeignKey(TenderComputeLine,null=True,blank=True,on_delete=models.SET_NULL); work_unit=models.ForeignKey(TenderComputeWorkUnit,null=True,blank=True,on_delete=models.SET_NULL)
    status=models.CharField(max_length=24,default="open"); engine_key=models.CharField(max_length=100,blank=True); question=models.TextField(); reason=models.TextField(blank=True); confidence=models.DecimalField(max_digits=5,decimal_places=4,null=True,blank=True); context=models.JSONField(default=dict,blank=True); possible_answers=models.JSONField(default=dict,blank=True); created_at=models.DateTimeField(auto_now_add=True); answered_at=models.DateTimeField(null=True,blank=True)
class OwnerFeedbackEvent(models.Model):
    interaction=models.ForeignKey(OwnerInteraction,on_delete=models.CASCADE,related_name="feedback_events"); actor=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.SET_NULL); raw_text=models.TextField(blank=True); payload=models.JSONField(default=dict,blank=True); scope=models.CharField(max_length=64,default="current_tender"); created_at=models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("OwnerFeedbackEvent is immutable")
        return super().save(*args, **kwargs)
class KnowledgeRecord(models.Model):
    feedback_event=models.ForeignKey(OwnerFeedbackEvent,null=True,blank=True,on_delete=models.SET_NULL); scope_type=models.CharField(max_length=64); scope_context=models.JSONField(default=dict,blank=True); applicability=models.JSONField(default=dict,blank=True); payload=models.JSONField(default=dict,blank=True); confidence=models.DecimalField(max_digits=5,decimal_places=4,null=True,blank=True); status=models.CharField(max_length=24,default="draft"); supersedes=models.ForeignKey("self",null=True,blank=True,on_delete=models.SET_NULL); created_at=models.DateTimeField(auto_now_add=True); updated_at=models.DateTimeField(auto_now=True)