import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0002_profile_avatar_content_type_profile_avatar_data_and_more")]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="background_updated_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.CreateModel(
            name="ProfileBackground",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("image_data", models.BinaryField(editable=False, verbose_name="Фон")),
                ("content_type", models.CharField(default="image/webp", editable=False, max_length=40)),
                ("profile", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="background_file", to="core.profile")),
            ],
            options={"verbose_name": "Фон пользователя", "verbose_name_plural": "Фоны пользователей"},
        ),
    ]
