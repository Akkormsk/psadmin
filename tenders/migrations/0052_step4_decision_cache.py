from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("tenders", "0051_catalog_product_name_gin_index"),
    ]

    operations = [
        migrations.CreateModel(
            name="Step4DecisionCache",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_signature", models.CharField(max_length=500, verbose_name="Нормализованный запрос")),
                ("product_external_id", models.CharField(max_length=100, verbose_name="ID товара поставщика")),
                ("candidate_signature", models.CharField(max_length=64, verbose_name="Хеш названия кандидата")),
                ("decision", models.CharField(choices=[("pass", "Подходит"), ("reject", "Не подходит")], max_length=8, verbose_name="Решение")),
                ("model_name", models.CharField(max_length=120, verbose_name="Модель")),
                ("contract_version", models.CharField(max_length=64, verbose_name="Версия контракта")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлено")),
                ("last_used_at", models.DateTimeField(blank=True, null=True, verbose_name="Последнее использование")),
                ("last_verified_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="Последняя проверка")),
                ("hit_count", models.PositiveIntegerField(default=0, verbose_name="Попаданий в кэш")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="step4_decision_cache_entries", to="tenders.catalogsupplier")),
            ],
            options={
                "verbose_name": "Решение кэша шага 4",
                "verbose_name_plural": "Решения кэша шага 4",
            },
        ),
        migrations.AddConstraint(
            model_name="step4decisioncache",
            constraint=models.UniqueConstraint(
                fields=("target_signature", "supplier", "product_external_id", "candidate_signature", "contract_version"),
                name="unique_step4_decision_cache_identity",
            ),
        ),
    ]
