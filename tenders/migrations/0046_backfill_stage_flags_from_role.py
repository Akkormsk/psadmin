from django.db import migrations


ROLE_TO_FLAGS = {
    # supply: изделие/материал без собственного производства — никогда не
    # завершает маршрут само по себе (нанесение обычно идёт следующим шагом).
    "supply": {"supplies_input": True, "performs_production": False, "terminal_mode": "sometimes"},
    # production: сама операция — может быть и промежуточной, и последней.
    "production": {"supplies_input": False, "performs_production": True, "terminal_mode": "sometimes"},
    # completion: упаковка/доставка — всегда завершает маршрут.
    "completion": {"supplies_input": False, "performs_production": False, "terminal_mode": "always"},
}


def backfill(apps, schema_editor):
    ProcessDefinition = apps.get_model("tenders", "ProcessDefinition")
    for role, flags in ROLE_TO_FLAGS.items():
        ProcessDefinition.objects.filter(role=role).update(**flags)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("tenders", "0045_lesson_source_processdefinition_parameters_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
