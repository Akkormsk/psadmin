from django.db import migrations


CATALOG_ROWS = [
    # Бумага
    ("paper", "Немел (Мастепринт) SRA3 120г", "лист", "3.00"),
    ("paper", "Меловка 90 SRA3", "лист", "3.30"),
    ("paper", "Меловка 130 SRA3", "лист", "5.80"),
    ("paper", "Меловка 150 SRA3", "лист", "6.00"),
    ("paper", "Меловка 200 SRA3", "лист", "8.00"),
    ("paper", "Меловка 250 SRA3", "лист", "10.50"),
    ("paper", "Меловка 350 SRA3", "лист", "15.20"),
    ("paper", "COLOR COPY SRA3 200", "лист", "17.50"),
    ("paper", "COLOR COPY SRA3 300", "лист", "32.80"),
    ("paper", "Лён ICELASER 300г SRA3", "лист", "58.40"),
    ("paper", "TOUCHE`COVER SRA3 300", "лист", "90.00"),
    ("paper", "Ritrama (с/к) SRA3 пг", "лист", "30.60"),
    ("paper", "Плёнка для цифровой печати SRA3", "лист", "114.00"),
    ("paper", "Oracal (с/к) SRA3", "лист", "42.50"),
    ("paper", "MAJESTIC SRA3 290г", "лист", "89.00"),
    ("paper", "HPG MIXED Kraft 295гр", "лист", "33.00"),
    ("paper", "Curious Metallics 300г белое серебро", "лист", "86.70"),
    ("paper", "КАЛЬКА ZANDERS SPECTRAL 200г", "лист", "103.00"),
    # Печать Konica
    ("konica", "CMYK A4 Заливка 30–100% (Картинки)", "страница", "4.70"),
    ("konica", "CMYK A4 0–30% (Текст, цветные схемы)", "страница", "2.70"),
    ("konica", "Ч/Б SRA3 0–30% (Текст, таблицы)", "страница", "0.50"),
    ("konica", "Ч/Б A4 30–100% (ч/б картинки)", "страница", "1.40"),
    ("konica", "Ч/Б A4 0–30% (Текст, таблицы)", "страница", "0.20"),
    # Печать Xerox
    ("xerox", "CMYK A3", "страница", "14.80"),
    ("xerox", "Ч/Б SRA3", "страница", "8.584"),
    ("xerox", "Ч/Б A3", "страница", "7.40"),
    ("xerox", "Ч/Б A4", "страница", "3.70"),
    # Постпечатная обработка
    ("postpress", "Ламинация пакет A4 200мкм", "ед.", "16.26"),
    ("postpress", "Ламинация рул. UltraBond SRA3 28мкм", "ед.", "8.00"),
    ("postpress", "Ламинация рул. SoftTouch SRA3 35мкм", "ед.", "12.00"),
    ("postpress", "Пружина в бобине, 30см", "ед.", "3.00"),
    ("postpress", "Тонкая пружина +-30см", "ед.", "10.00"),
    ("postpress", "Толстая пружина +-30см", "ед.", "16.00"),
    ("postpress", "Люверс", "ед.", "2.00"),
    ("postpress", "КБС", "ед.", "20.00"),
    ("postpress", "Плоттерная р. SRA3 тонк.", "ед.", "3.00"),
    ("postpress", "Плоттерная р. SRA3 толст.", "ед.", "16.00"),
    ("postpress", "Монтажная пленка 50×100см", "ед.", "60.00"),
    ("postpress", "Обложка прозрачная A3", "ед.", "8.70"),
    ("postpress", "Обложка прозрачная A4", "ед.", "5.00"),
    ("postpress", "2-стор. скотч 1×10см", "ед.", "1.00"),
    ("postpress", "Пакет упаковочный с клапаном", "ед.", "3.00"),
]


def expand_catalog(apps, schema_editor):
    Item = apps.get_model("calculator", "PriceItem")
    for category, name, unit_name, price in CATALOG_ROWS:
        Item.objects.get_or_create(
            category=category,
            name=name,
            defaults={"unit_name": unit_name, "unit_price": price},
        )


class Migration(migrations.Migration):
    dependencies = [("calculator", "0003_estimate_summary_snapshot")]

    operations = [migrations.RunPython(expand_catalog, migrations.RunPython.noop)]
