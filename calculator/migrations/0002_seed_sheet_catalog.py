from django.db import migrations


def seed_catalog(apps, schema_editor):
    Settings = apps.get_model("calculator", "CalculatorSettings")
    Item = apps.get_model("calculator", "PriceItem")
    Settings.objects.get_or_create(pk=1)
    rows = [
        ("paper", "Обычная A4 80", "лист", "0.80"), ("paper", "Maestro Special A3 80", "лист", "1.70"),
        ("paper", "Меловка 170 SRA3", "лист", "7.50"), ("paper", "Меловка 300 SRA3", "лист", "12.00"),
        ("konica", "CMYK SRA3 Заливка 50–100% (Картинки)", "страница", "12.50"), ("konica", "CMYK SRA3 Заливка 0–50% (Текст)", "страница", "7.40"),
        ("xerox", "CMYK SRA3", "страница", "17.168"), ("xerox", "CMYK A4", "страница", "7.40"),
        ("postpress", "Скоба", "ед.", "2.00"), ("postpress", "Ламинация пакет A3 150мкм", "ед.", "20.00"),
        ("postpress", "Фольгирование SRA3", "ед.", "20.00"), ("embossing", "Приладка (количество клише)", "клише", "1000.00"), ("embossing", "Ударов", "удар", "4.00"),
    ]
    for category, name, unit_name, price in rows:
        Item.objects.get_or_create(category=category, name=name, defaults={"unit_name": unit_name, "unit_price": price})


class Migration(migrations.Migration):
    dependencies = [("calculator", "0001_initial")]
    operations = [migrations.RunPython(seed_catalog, migrations.RunPython.noop)]
