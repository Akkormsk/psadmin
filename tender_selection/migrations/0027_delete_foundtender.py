from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [("tender_selection", "0026_remove_foundtender")]

    operations = [migrations.DeleteModel(name="FoundTender")]
