# Убирает смысловой индекс каталога (CatalogProduct.embedding*), добавленный
# 0028: один раз включённый в общем конфиге (CascadeConfigVersion) шаг 3
# лаборатории «semantic=yes» затронул ВСЕ боевые прогоны, не только
# лабораторию — на 1 CPU/1 ГБ проде это положило CPU в 100% (авария
# 15.09.2026, см. docs/assistant_protocol.md). Решение — не просто
# отключить (переменной окружения), а убрать полностью из прода: код,
# поле, шлюзовую команду.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0028_catalogproduct_embedding_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='catalogproduct',
            name='embedding',
        ),
        migrations.RemoveField(
            model_name='catalogproduct',
            name='embedding_model',
        ),
        migrations.RemoveField(
            model_name='catalogproduct',
            name='embedding_text_hash',
        ),
        migrations.RemoveField(
            model_name='catalogproduct',
            name='embedding_updated_at',
        ),
    ]
