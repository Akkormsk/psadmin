from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("calculator", "0007_priceitem_admin_proxies")]

    operations = [
        migrations.AddField(
            model_name="estimate",
            name="comment",
            field=models.CharField(blank=True, max_length=300, verbose_name="Комментарий"),
        ),
    ]
