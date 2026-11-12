from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0039_alter_profile_triage_labels")]
    operations = [migrations.CreateModel(
        name="DiscoveryCursor",
        fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("query_key", models.CharField("Отпечаток первичного фильтра", max_length=64, unique=True)),
                ("through_at", models.DateTimeField("Подтверждённая граница обновления источника"))],
        options={"verbose_name": "Курсор первичного сбора", "verbose_name_plural": "Курсоры первичного сбора"},
    )]
