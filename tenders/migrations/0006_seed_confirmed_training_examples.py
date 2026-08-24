import json
from pathlib import Path

from django.db import migrations


def seed_confirmed_examples(apps, schema_editor):
    User = apps.get_model("auth", "User")
    ProductionType = apps.get_model("tenders", "ProductionType")
    ProductionTrainingExample = apps.get_model("tenders", "ProductionTrainingExample")

    creator = (
        User.objects.filter(username__iexact="Admin").first()
        or User.objects.filter(is_superuser=True).first()
        or User.objects.filter(is_staff=True).first()
        or User.objects.first()
    )
    if creator is None:
        return

    seed_path = Path(__file__).resolve().parent.parent / "seed_training_examples.json"
    examples = json.loads(seed_path.read_text(encoding="utf-8"))
    for data in examples:
        production_type = ProductionType.objects.get(code=data["production_type"])
        existing = ProductionTrainingExample.objects.filter(
            production_type=production_type,
            position_name=data["position_name"],
        )
        if any(value.features == data["features"] and value.routes == data["routes"] for value in existing):
            continue
        ProductionTrainingExample.objects.create(
            production_type=production_type,
            position_name=data["position_name"],
            requirements=data.get("requirements", {}),
            features=data.get("features", []),
            routes=data.get("routes", []),
            note=data.get("note", ""),
            created_by=creator,
        )


class Migration(migrations.Migration):
    dependencies = [("tenders", "0005_productiontrainingsession_productiontrainingturn")]
    operations = [migrations.RunPython(seed_confirmed_examples, migrations.RunPython.noop)]
