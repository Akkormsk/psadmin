from django.db import migrations, models


def order_catalog(apps, schema_editor):
    Item = apps.get_model("calculator", "PriceItem")
    Item.objects.filter(
        category__in=["wide_paper", "wide_print", "wide_postpress"],
        name="Другое",
    ).delete()
    categories = [
        "paper", "konica", "xerox", "postpress", "embossing",
        "wide_paper", "wide_print", "wide_postpress",
    ]
    for category in categories:
        for position, item in enumerate(Item.objects.filter(category=category).order_by("pk"), start=1):
            item.sort_order = position * 10
            item.save(update_fields=["sort_order"])


class Migration(migrations.Migration):
    dependencies = [("calculator", "0005_add_wide_format_calculator")]

    operations = [
        migrations.AddField(
            model_name="priceitem",
            name="sort_order",
            field=models.PositiveIntegerField(default=0, verbose_name="Порядок"),
        ),
        migrations.AlterModelOptions(
            name="priceitem",
            options={
                "ordering": ["category", "sort_order", "pk"],
                "verbose_name": "Позиция калькулятора",
                "verbose_name_plural": "Справочник калькулятора",
            },
        ),
        migrations.AlterField(
            model_name="estimate",
            name="calculator_type",
            field=models.CharField(
                choices=[("sheet", "Листовая печать"), ("wide", "Плоттер Canon")],
                default="sheet",
                max_length=20,
                verbose_name="Калькулятор",
            ),
        ),
        migrations.AlterField(
            model_name="priceitem",
            name="category",
            field=models.CharField(
                choices=[
                    ("paper", "Бумага"),
                    ("konica", "Печать · Konica"),
                    ("xerox", "Печать · Xerox"),
                    ("postpress", "Постпечатная обработка"),
                    ("embossing", "Тиснение"),
                    ("wide_paper", "Плоттер Canon · Бумага"),
                    ("wide_print", "Плоттер Canon · Печать"),
                    ("wide_postpress", "Плоттер Canon · Постпечатка"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="estimateline",
            name="category",
            field=models.CharField(
                choices=[
                    ("paper", "Бумага"),
                    ("konica", "Печать · Konica"),
                    ("xerox", "Печать · Xerox"),
                    ("postpress", "Постпечатная обработка"),
                    ("embossing", "Тиснение"),
                    ("wide_paper", "Плоттер Canon · Бумага"),
                    ("wide_print", "Плоттер Canon · Печать"),
                    ("wide_postpress", "Плоттер Canon · Постпечатка"),
                ],
                max_length=20,
            ),
        ),
        migrations.RunPython(order_catalog, migrations.RunPython.noop),
    ]
