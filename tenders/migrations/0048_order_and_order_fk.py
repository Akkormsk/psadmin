import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0047_alter_orderestimate_status_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='Order',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=300, verbose_name='Название')),
                ('status', models.CharField(choices=[('draft', 'Черновик'), ('pending', 'На торгах'), ('not_participated', 'Не участвовали'), ('lost', 'Проигран'), ('won', 'Выигран')], default='draft', max_length=16, verbose_name='Статус')),
                ('archived_at', models.DateTimeField(blank=True, null=True, verbose_name='В архиве с')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Заказ',
                'verbose_name_plural': 'Заказы',
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddField(
            model_name='orderestimate',
            name='order',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='estimates', to='tenders.order', verbose_name='Заказ'),
        ),
    ]
