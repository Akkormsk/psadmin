from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0047_backfill_tender_history")]

    # DDL runs after the data migration commits its deferred foreign-key checks.
    operations = [
        migrations.AddConstraint(
            model_name="tenderhistory",
            constraint=models.UniqueConstraint(fields=["source", "law", "purchase_number"], name="uniq_tender_history_identity"),
        ),
    ]
