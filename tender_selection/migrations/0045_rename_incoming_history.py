from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0044_discoverycursor")]

    operations = [
        migrations.AlterModelTable(name="incomingtrace", table="tender_selection_incomingtrace"),
        migrations.RenameModel(old_name="IncomingTrace", new_name="TenderHistory"),
    ]
