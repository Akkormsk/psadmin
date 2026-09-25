from django.db import migrations


def backfill_manual_estimate_cards(apps, schema_editor):
    # Реальный перенос выполняет 0025, когда у Tender уже есть все поля
    # жизненного цикла. Здесь оставлена безопасная no-op миграция, потому что
    # эта ревизия не должна создавать промежуточные FoundTender.
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("tender_selection", "0021_foundtender_archived_at_alter_foundtender_review"),
        ("tenders", "0034_tenderestimate_archived_at"),
    ]

    operations = [
        migrations.RunPython(backfill_manual_estimate_cards, migrations.RunPython.noop),
    ]
