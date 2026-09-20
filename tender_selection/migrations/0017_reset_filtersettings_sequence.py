from django.core.management.color import no_style
from django.db import migrations


def reset_sequence(apps, schema_editor):
    """Миграции 0013/0014 сеяли FilterSettings через явный pk=1, не через
    автоинкремент — последовательность id_seq не узнала об этой строке и
    следующий .create() пытается снова взять id=1 (см. тот же баг у
    calculator.CalculatorSettings, миграция 0011 в том приложении)."""
    FilterSettings = apps.get_model("tender_selection", "FilterSettings")
    with schema_editor.connection.cursor() as cursor:
        for sql in schema_editor.connection.ops.sequence_reset_sql(no_style(), [FilterSettings]):
            cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0016_foundtender_opened_at")]
    operations = [migrations.RunPython(reset_sequence, migrations.RunPython.noop)]
