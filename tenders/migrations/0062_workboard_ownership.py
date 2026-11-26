from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("tenders", "0061_tender_data_foundation")]

    operations = [
        migrations.CreateModel(
            name="Task",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=300, verbose_name="Название")),
                ("description", models.TextField(blank=True, verbose_name="Описание")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создана")),
            ],
        ),
        migrations.AlterField(
            model_name="tendercommercialitem", name="tender",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="v2_commercial_items", to="tender_selection.tender"),
        ),
        migrations.AddField(
            model_name="tendercommercialitem", name="order",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="commercial_items", to="tenders.order"),
        ),
        migrations.AddField(
            model_name="tendercommercialitem", name="task",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="commercial_items", to="tenders.task"),
        ),
        migrations.AddConstraint(
            model_name="tendercommercialitem",
            constraint=models.UniqueConstraint(fields=["order", "source_key"], name="unique_order_commercial_item"),
        ),
        migrations.AddConstraint(
            model_name="tendercommercialitem",
            constraint=models.UniqueConstraint(fields=["task", "source_key"], name="unique_task_commercial_item"),
        ),
        migrations.AddConstraint(
            model_name="tendercommercialitem",
            constraint=models.CheckConstraint(condition=(
                models.Q(tender__isnull=False, order__isnull=True, task__isnull=True)
                | models.Q(tender__isnull=True, order__isnull=False, task__isnull=True)
                | models.Q(tender__isnull=True, order__isnull=True, task__isnull=False)
            ), name="commercial_item_exactly_one_owner"),
        ),
        migrations.AddConstraint(
            model_name="tendercommercialitem",
            constraint=models.CheckConstraint(condition=models.Q(tender__isnull=False) | models.Q(job__isnull=True), name="commercial_item_tender_job_only"),
        ),
    ]
