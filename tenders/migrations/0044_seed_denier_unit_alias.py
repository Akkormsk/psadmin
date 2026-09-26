# Написания денье («D», «den», «ден», «денье») — популярная единица прочности
# нити у тканевых сумок/рюкзаков/дождевиков («210D»), встречающаяся и в
# самих строках ТЗ. Это НЕ конвертация в г/м² (площадная плотность ткани —
# другая физическая величина, денье измеряет линейную плотность нити и
# однозначного пересчёта не имеет — см. docs/backlog/cascade-step5-matching.md,
# п.2) — только сведение разных написаний ОДНОЙ и той же единицы денье к
# одному каноническому виду, тот же паттерн, что и 0031_unit_alias.py.
from django.db import migrations

_SEED = {
    "d": "d",
    "den": "d",
    "ден": "d",
    "денье": "d",
    "denier": "d",
}


def seed_denier_alias(apps, schema_editor):
    UnitAlias = apps.get_model("tenders", "UnitAlias")
    UnitAlias.objects.bulk_create(
        [UnitAlias(spelling=spelling, canonical=canonical) for spelling, canonical in _SEED.items()],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0043_attribute_concept_hint'),
    ]

    operations = [
        migrations.RunPython(seed_denier_alias, migrations.RunPython.noop),
    ]
