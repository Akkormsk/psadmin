from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


TYPES = [
    ("digital_sheet", "Цифровая листовая печать", "Небольшие тиражи листовой полиграфии без сложной фигурной вырубки"),
    ("wide_format", "Широкоформатная печать", "Рулонная и крупноформатная печать"),
    ("offset_print", "Офсетная полиграфия", "Средние и большие тиражи офсетной печати"),
    ("die_cut_assembly", "Сложная вырубка и сборка", "Пакеты, коробки и конструкции с вырубкой, склейкой или сборкой"),
    ("promo_with_branding", "Готовая сувенирная продукция с нанесением", "Готовые изделия из каталогов с последующим нанесением"),
    ("textile_merch", "Текстиль и мерч", "Готовый или пошивной текстиль с нанесением"),
    ("binding_special", "Переплётное и специализированное производство", "Твёрдый переплёт, адресные папки и специализированные изделия"),
    ("other", "Другой тип производства", "Тип, которого пока нет в классификаторе"),
]


def seed_types(apps, schema_editor):
    ProductionType = apps.get_model("tenders", "ProductionType")
    for order, (code, name, description) in enumerate(TYPES, 10):
        ProductionType.objects.get_or_create(code=code, defaults={"name": name, "description": description, "sort_order": order})


class Migration(migrations.Migration):
    dependencies = [("tenders", "0002_tenderestimate_document_analysis_and_more"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name="ProductionType",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.SlugField(max_length=80, unique=True, verbose_name="Код")),
                ("name", models.CharField(max_length=200, verbose_name="Тип производства")),
                ("description", models.CharField(blank=True, max_length=500, verbose_name="Краткие признаки")),
                ("sort_order", models.PositiveIntegerField(default=0, verbose_name="Порядок")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активен")),
            ],
            options={"verbose_name": "Тип производства", "verbose_name_plural": "Типы производства", "ordering": ["sort_order", "pk"]},
        ),
        migrations.CreateModel(
            name="ProductionTrainingExample",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position_name", models.CharField(max_length=500, verbose_name="Наименование позиции")),
                ("requirements", models.JSONField(blank=True, default=dict, verbose_name="Исходные требования")),
                ("features", models.JSONField(blank=True, default=list, verbose_name="Существенные признаки")),
                ("note", models.CharField(blank=True, max_length=500, verbose_name="Комментарий администратора")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="production_training_examples", to=settings.AUTH_USER_MODEL)),
                ("production_type", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="examples", to="tenders.productiontype", verbose_name="Подтверждённый тип")),
            ],
            options={"verbose_name": "Учебный пример производства", "verbose_name_plural": "Учебные примеры производства", "ordering": ["-created_at"]},
        ),
        migrations.RunPython(seed_types, migrations.RunPython.noop),
    ]
