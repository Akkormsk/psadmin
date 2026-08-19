from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("calculator", "0006_order_catalog_and_rename_canon")]

    operations = [
        migrations.CreateModel(
            name="SheetPriceItem",
            fields=[],
            options={
                "verbose_name": "Цена листовой печати",
                "verbose_name_plural": "Листовая печать — цены",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("calculator.priceitem",),
        ),
        migrations.CreateModel(
            name="CanonPriceItem",
            fields=[],
            options={
                "verbose_name": "Цена плоттера Canon",
                "verbose_name_plural": "Плоттер Canon — цены",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("calculator.priceitem",),
        ),
    ]
