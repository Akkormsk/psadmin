from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("tenders", "0064_workboards"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name="workboardcard", name="import_key", field=models.CharField(blank=True, editable=False, max_length=64, null=True, verbose_name="Ключ импорта")),
        migrations.AddConstraint(model_name="workboardcard", constraint=models.UniqueConstraint(fields=("board", "import_key"), name="unique_workboard_import_key")),
        migrations.AddField(model_name="tendercommercialitem", name="description", field=models.TextField(blank=True, default="", verbose_name="Описание"), preserve_default=False),
        migrations.AlterField(model_name="componentoperationstep", name="process", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="v2_operation_steps", to="tenders.processdefinition")),
        migrations.AddField(model_name="componentoperationstep", name="manual_name", field=models.CharField(blank=True, default="", max_length=300, verbose_name="Название ручного этапа")),
        migrations.AddConstraint(model_name="componentoperationstep", constraint=models.CheckConstraint(condition=models.Q(models.Q(("manual_name", ""), ("process__isnull", False)), models.Q(("process__isnull", True), models.Q(("manual_name", ""), _negated=True)), _connector="OR"), name="v2_step_process_or_manual_name")),
        migrations.CreateModel(name="WorkCheckpoint", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("name", models.CharField(max_length=300, verbose_name="Критерий выполнения")),
            ("required", models.BooleanField(default=True, verbose_name="Обязательный")),
            ("position", models.PositiveIntegerField(verbose_name="Порядок")),
            ("completed_at", models.DateTimeField(blank=True, null=True, verbose_name="Выполнен")),
            ("completed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL, verbose_name="Кто выполнил")),
            ("step", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="checkpoints", to="tenders.componentoperationstep", verbose_name="Этап")),
        ], options={"ordering": ["position", "pk"]}),
        migrations.AddConstraint(model_name="workcheckpoint", constraint=models.UniqueConstraint(fields=("step", "position"), name="unique_work_checkpoint_position")),
        migrations.CreateModel(name="WorkCheckpointEvent", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("completed", models.BooleanField(verbose_name="Отметка выполнения")),
            ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Время")),
            ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL, verbose_name="Пользователь")),
            ("checkpoint", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="events", to="tenders.workcheckpoint", verbose_name="Критерий")),
        ]),
    ]
