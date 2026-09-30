from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0049_copy_outcome_to_tender_and_order'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='orderestimate',
            name='status',
        ),
        migrations.RemoveField(
            model_name='tenderestimate',
            name='actual_price',
        ),
        migrations.RemoveField(
            model_name='tenderestimate',
            name='actual_reduction_percent',
        ),
        migrations.RemoveField(
            model_name='tenderestimate',
            name='archived_at',
        ),
        migrations.RemoveField(
            model_name='tenderestimate',
            name='bid_number',
        ),
        migrations.RemoveField(
            model_name='tenderestimate',
            name='bid_price',
        ),
        migrations.RemoveField(
            model_name='tenderestimate',
            name='outcome_checked_at',
        ),
        migrations.RemoveField(
            model_name='tenderestimate',
            name='outcome_source',
        ),
        migrations.RemoveField(
            model_name='tenderestimate',
            name='protocol',
        ),
        migrations.RemoveField(
            model_name='tenderestimate',
            name='protocol_checked_at',
        ),
        migrations.RemoveField(
            model_name='tenderestimate',
            name='status',
        ),
        migrations.AlterField(
            model_name='tenderestimate',
            name='result_notes',
            field=models.TextField(blank=True, verbose_name='Комментарий'),
        ),
    ]
