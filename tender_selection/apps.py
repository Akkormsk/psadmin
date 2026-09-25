from django.apps import AppConfig


class TenderSelectionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tender_selection'
    verbose_name = "Тендеры"

    def ready(self):
        from . import scheduler
        scheduler.start()
