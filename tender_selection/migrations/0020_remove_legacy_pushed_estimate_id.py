from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tender_selection', '0019_backfill_tender_links'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='foundtender',
            name='_legacy_pushed_estimate_id',
        ),
    ]
