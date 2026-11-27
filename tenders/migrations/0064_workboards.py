from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("tender_selection", "0051_active_tender_data_writes"),
        ("tenders", "0063_workboard_component_details"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkBoard",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200, verbose_name="Название")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создана")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлена")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="workboards", to=settings.AUTH_USER_MODEL, verbose_name="Создал")),
            ],
            options={"verbose_name": "Доска", "verbose_name_plural": "Доски", "ordering": ["name", "pk"]},
        ),
        migrations.CreateModel(
            name="WorkBoardColumn",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="Название")),
                ("kind", models.CharField(choices=[("queue", "В очереди"), ("standard", "Обычная"), ("done", "Готово")], default="standard", max_length=16, verbose_name="Назначение")),
                ("position", models.PositiveIntegerField(verbose_name="Порядок")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создана")),
                ("board", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="columns", to="tenders.workboard", verbose_name="Доска")),
            ],
            options={"verbose_name": "Колонка доски", "verbose_name_plural": "Колонки доски", "ordering": ["position", "pk"]},
        ),
        migrations.CreateModel(
            name="WorkBoardCard",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveIntegerField(default=0, verbose_name="Порядок")),
                ("completed_at", models.DateTimeField(blank=True, null=True, verbose_name="Завершена")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создана")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлена")),
                ("board", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cards", to="tenders.workboard", verbose_name="Доска")),
                ("column", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="cards", to="tenders.workboardcolumn", verbose_name="Колонка")),
                ("task", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="workboard_card", to="tenders.task", verbose_name="Задача")),
                ("tender", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="workboard_card", to="tender_selection.tender", verbose_name="Тендер")),
            ],
            options={"verbose_name": "Карточка доски", "verbose_name_plural": "Карточки доски", "ordering": ["position", "pk"]},
        ),
        migrations.AddConstraint(
            model_name="workboardcolumn",
            constraint=models.UniqueConstraint(fields=("board", "position"), name="unique_workboard_column_position"),
        ),
        migrations.AddConstraint(
            model_name="workboardcolumn",
            constraint=models.UniqueConstraint(condition=models.Q(("kind", "queue")), fields=("board", "kind"), name="unique_workboard_queue_column"),
        ),
        migrations.AddConstraint(
            model_name="workboardcolumn",
            constraint=models.UniqueConstraint(condition=models.Q(("kind", "done")), fields=("board", "kind"), name="unique_workboard_done_column"),
        ),
        migrations.AddConstraint(
            model_name="workboardcard",
            constraint=models.CheckConstraint(condition=models.Q(models.Q(("task__isnull", False), ("tender__isnull", True)), models.Q(("task__isnull", True), ("tender__isnull", False)), _connector="OR"), name="workboard_card_exactly_one_subject"),
        ),
    ]
