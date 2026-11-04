from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tender_selection', '0040_merge_profile_and_intake')]
    operations = [migrations.AddField(
        model_name='tender', name='profile_assessment',
        field=models.JSONField('Основания проверки профиля', default=dict, blank=True,
            help_text='Решение агента, подтверждающие фрагменты текущего извещения и версия проверки. Не изменяет ручные решения.'),
    )]
