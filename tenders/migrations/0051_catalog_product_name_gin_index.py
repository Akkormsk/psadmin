from django.db import migrations


INDEX_NAME = "catalog_product_name_fts_gin"


def create_name_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "CREATE INDEX IF NOT EXISTS " + INDEX_NAME + " ON tenders_catalogproduct "
        "USING GIN (to_tsvector('russian', coalesce(name, '') || ' ' || coalesce(full_name, '')))"
    )


def drop_name_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP INDEX IF EXISTS " + INDEX_NAME)


class Migration(migrations.Migration):
    dependencies = [("tenders", "0050_remove_old_estimate_outcome_fields")]

    operations = [migrations.RunPython(create_name_index, drop_name_index)]
