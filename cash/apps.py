from django.apps import AppConfig


class CashConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cash'
    verbose_name = "Касса"

    def ready(self):
        from . import scheduler
        scheduler.start()
