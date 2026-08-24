from django.db import migrations, models


PROCESSES = [
    ("supply", "Поставка материала"),
    ("supply", "Поставка готового бланка"),
    ("production", "Цифровая листовая печать"),
    ("production", "Широкоформатная печать"),
    ("production", "Офсетная печать"),
    ("production", "Сложная вырубка и сборка"),
    ("production", "Пошив"),
    ("production", "Шелкография"),
    ("production", "DTF / УФ-DTF"),
    ("production", "Прямая УФ-печать"),
    ("production", "Вышивка"),
    ("production", "Тиснение"),
    ("completion", "Упаковка"),
    ("completion", "Логистика"),
]


def seed_processes(apps, schema_editor):
    ProcessDefinition = apps.get_model("tenders", "ProcessDefinition")
    for role, name in PROCESSES:
        ProcessDefinition.objects.get_or_create(role=role, name=name)


class Migration(migrations.Migration):
    dependencies = [("tenders", "0003_production_training")]
    operations = [
        migrations.AddField(
            model_name="productiontrainingexample",
            name="routes",
            field=models.JSONField(blank=True, default=list, verbose_name="Подтверждённые маршруты"),
        ),
        migrations.CreateModel(
            name="ProcessDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200, verbose_name="Процесс")),
                ("role", models.CharField(choices=[("supply", "Снабжение"), ("production", "Производство"), ("completion", "Завершение и логистика")], max_length=20, verbose_name="Роль")),
                ("description", models.CharField(blank=True, max_length=500, verbose_name="Когда применяется")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активен")),
            ],
            options={"verbose_name": "Процесс маршрута", "verbose_name_plural": "Процессы маршрутов", "ordering": ["role", "name"]},
        ),
        migrations.AddConstraint(
            model_name="processdefinition",
            constraint=models.UniqueConstraint(fields=("name", "role"), name="unique_tender_process_role"),
        ),
        migrations.RunPython(seed_processes, migrations.RunPython.noop),
    ]
