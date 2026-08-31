from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenders", "0013_training_knowledge_sync_and_embeddings"),
    ]

    operations = [
        migrations.AddField(
            model_name="catalogcategory",
            name="embedding",
            field=models.JSONField(blank=True, default=list, verbose_name="Смысловой индекс"),
        ),
        migrations.AddField(
            model_name="catalogcategory",
            name="embedding_model",
            field=models.CharField(blank=True, max_length=100, verbose_name="Модель смыслового индекса"),
        ),
        migrations.AddField(
            model_name="catalogcategory",
            name="embedding_text_hash",
            field=models.CharField(blank=True, max_length=64, verbose_name="Хеш смыслового представления"),
        ),
        migrations.AddField(
            model_name="catalogcategory",
            name="embedding_updated_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Индекс обновлён"),
        ),
    ]
