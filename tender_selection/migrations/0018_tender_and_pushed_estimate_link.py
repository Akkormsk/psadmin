from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tender_selection', '0017_reset_filtersettings_sequence'),
        ('tenders', '0032_tenderestimate_actual_price_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='Tender',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('law', models.CharField(choices=[('fz44', '44-ФЗ'), ('fz223', '223-ФЗ')], db_index=True, default='fz44', max_length=8, verbose_name='Закон')),
                ('purchase_number', models.CharField(db_index=True, max_length=40, verbose_name='Номер закупки')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создан')),
            ],
            options={
                'verbose_name': 'Тендер',
                'verbose_name_plural': 'Тендеры',
            },
        ),
        migrations.AddConstraint(
            model_name='tender',
            constraint=models.UniqueConstraint(fields=('law', 'purchase_number'), name='uniq_tender_law_purchase_number'),
        ),
        migrations.RenameField(
            model_name='foundtender',
            old_name='pushed_estimate_id',
            new_name='_legacy_pushed_estimate_id',
        ),
        migrations.AlterField(
            model_name='foundtender',
            name='_legacy_pushed_estimate_id',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='ID просчёта (устарело)'),
        ),
        migrations.AddField(
            model_name='foundtender',
            name='tender',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='found_tender', to='tender_selection.tender', verbose_name='Тендер (общий якорь)'),
        ),
        migrations.AddField(
            model_name='foundtender',
            name='pushed_estimate',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='found_tender', to='tenders.tenderestimate', verbose_name='Просчёт'),
        ),
    ]
