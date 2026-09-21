from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0032_tenderestimate_actual_price_and_more'),
        ('tender_selection', '0018_tender_and_pushed_estimate_link'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenderestimate',
            name='tender',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='estimates', to='tender_selection.tender', verbose_name='Тендер'),
        ),
    ]
