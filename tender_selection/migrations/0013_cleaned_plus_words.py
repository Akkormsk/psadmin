from django.db import migrations

# Плюс-слова в основах: собраны из прежнего списка форм, убраны дубли,
# исправлены пропущенные окончания (печатной, наградная, кружек, корпоративной).
PLUS_WORDS = (
    "брендированн, брендинг, сувенир, полиграф, печатн, печать, типографск, логотип, "
    "мерч, фирменн, корпоративн, имиджев, атрибут, наградн, подар, вышивк, гравировк, "
    "сублимац, шелкограф, нанесени, дизайн, ежедневник, планинг, календар, блокнот, "
    "папк, папок, открытк, открыток, шоппер, кружк, кружек, термос, бейдж, футболк, "
    "футболок, толстовк, толстовок, кепк, кепок, худи, лонгслив, павербанк, powerbank"
)


def set_plus_words(apps, schema_editor):
    FilterSettings = apps.get_model("tender_selection", "FilterSettings")
    obj, _ = FilterSettings.objects.get_or_create(pk=1)
    obj.include_words = PLUS_WORDS
    obj.save(update_fields=["include_words"])


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0012_contractstat_customer_inn")]
    operations = [migrations.RunPython(set_plus_words, migrations.RunPython.noop)]
