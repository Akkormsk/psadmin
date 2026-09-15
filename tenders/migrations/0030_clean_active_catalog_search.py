from copy import deepcopy

from django.db import migrations


def clean_active_search(apps, schema_editor):
    versions = apps.get_model("tenders", "CascadeConfigVersion").objects.using(schema_editor.connection.alias)
    for previous in list(versions.filter(is_active=True)):
        settings = deepcopy(previous.settings)
        steps = settings.get("steps") if isinstance(settings, dict) else None
        if not isinstance(steps, dict) or not isinstance(steps.get("3"), dict):
            continue
        cleaned = {key: value for key, value in steps["3"].items() if key == "sources"}
        if cleaned == steps["3"]:
            continue
        steps["3"] = cleaned
        versions.filter(pk=previous.pk).update(is_active=False)
        versions.create(
            name=f"{previous.name[:160]} · поиск по названиям",
            created_by_id=previous.created_by_id, settings=settings, is_active=True,
        )


class Migration(migrations.Migration):
    dependencies = [("tenders", "0029_remove_catalogproduct_embedding_fields")]
    operations = [migrations.RunPython(clean_active_search, migrations.RunPython.noop)]
