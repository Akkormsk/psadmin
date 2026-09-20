from django.core.management.color import no_style
from django.db import migrations


def reset_sequence(apps, schema_editor):
    """Постгрес-миграция 0002 сеяла CalculatorSettings через явный pk=1, не
    через автоинкремент — последовательность id_seq не узнала об этой строке
    и следующий .create() пытается снова взять id=1 (SQLite так не делает,
    там id считается от MAX(id), поэтому баг был не виден локально до
    перехода на Postgres)."""
    Settings = apps.get_model("calculator", "CalculatorSettings")
    with schema_editor.connection.cursor() as cursor:
        for sql in schema_editor.connection.ops.sequence_reset_sql(no_style(), [Settings]):
            cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [("calculator", "0010_calculatorsettings_vat_rate")]
    operations = [migrations.RunPython(reset_sequence, migrations.RunPython.noop)]
