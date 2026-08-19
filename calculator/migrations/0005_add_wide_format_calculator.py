from django.db import migrations, models


WIDE_ITEMS = [
    ("wide_paper", "Plain paper 80g 594/610мм", "пм", "13.72"),
    ("wide_paper", "Plain paper 80g 841/914мм", "пм", "18.64"),
    ("wide_paper", "Matt coated paper 180g 594/610мм", "пм", "85.80"),
    ("wide_paper", "Matt coated paper 180g 841/914мм", "пм", "112.67"),
    ("wide_paper", "Другое", "пм", "1.00"),
    ("wide_print", "594/610мм Чертежи/Лекала", "пм", "4.20"),
    ("wide_print", "841/914мм Чертежи/Лекала", "пм", "6.00"),
    ("wide_print", "594/610мм Плотная заливка", "пм", "42.00"),
    ("wide_print", "841/914мм Плотная заливка", "пм", "60.00"),
    ("wide_print", "Другое", "пм", "1.00"),
    ("wide_postpress", "Фальцовка по ГОСТ", "ед.", "20.00"),
    ("wide_postpress", "Брошюровка на пружину", "ед.", "15.00"),
    ("wide_postpress", "Другое", "ед.", "5.00"),
]


def seed_wide_items(apps, schema_editor):
    Item = apps.get_model("calculator", "PriceItem")
    for category, name, unit_name, price in WIDE_ITEMS:
        Item.objects.get_or_create(
            category=category,
            name=name,
            defaults={"unit_name": unit_name, "unit_price": price},
        )


class Migration(migrations.Migration):
    dependencies = [("calculator", "0004_expand_sheet_catalog")]

    operations = [
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
                    ("wide_paper", "Широкоформатная печать · Бумага"),
                    ("wide_print", "Широкоформатная печать · Печать"),
                    ("wide_postpress", "Широкоформатная печать · Постпечатка"),
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
                    ("wide_paper", "Широкоформатная печать · Бумага"),
                    ("wide_print", "Широкоформатная печать · Печать"),
                    ("wide_postpress", "Широкоформатная печать · Постпечатка"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="estimate",
            name="calculator_type",
            field=models.CharField(
                choices=[("sheet", "Листовая печать"), ("wide", "Широкоформатная печать")],
                default="sheet",
                max_length=20,
                verbose_name="Калькулятор",
            ),
        ),
        migrations.RunPython(seed_wide_items, migrations.RunPython.noop),
    ]
