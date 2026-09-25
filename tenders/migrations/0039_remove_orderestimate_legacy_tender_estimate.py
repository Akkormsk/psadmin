from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [("tenders", "0038_detach_orders_from_tenders")]

    operations = [
        migrations.RemoveField(
            model_name="orderestimate",
            name="legacy_tender_estimate",
        ),
    ]
