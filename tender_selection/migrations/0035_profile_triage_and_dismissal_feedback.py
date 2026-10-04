from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0034_tender_contract_winner_inn")]

    operations = [
        migrations.AddField(
            model_name="filtersettings",
            name="profile_triage_enabled",
            field=models.BooleanField(default=False, help_text="Проверяет только новые тендеры, уже прошедшие плюс/минус-слова.", verbose_name="Jev: помечать сомнительные входящие"),
        ),
        migrations.AddField(
            model_name="tender",
            name="profile_checked_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Jev проверил профиль"),
        ),
        migrations.AddField(
            model_name="tender",
            name="profile_confidence",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=4, null=True, verbose_name="Уверенность Jev по профилю"),
        ),
        migrations.AddField(
            model_name="tender",
            name="profile_signal",
            field=models.CharField(blank=True, choices=[("", "Не проверен"), ("clear", "Похоже, по профилю"), ("doubt", "Нужно проверить"), ("not_profile", "Возможно, не по профилю")], max_length=16, verbose_name="Сигнал Jev по профилю"),
        ),
        migrations.CreateModel(
            name="TenderDismissalFeedback",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reason", models.CharField(choices=[("not_profile", "Не по профилю"), ("other", "Другая причина")], max_length=16, verbose_name="Причина")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Когда отмечено")),
                ("tender", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dismissal_feedback", to="tender_selection.tender")),
            ],
            options={"verbose_name": "Причина скрытия входящего", "verbose_name_plural": "Причины скрытия входящих", "ordering": ["-created_at", "-pk"]},
        ),
    ]
