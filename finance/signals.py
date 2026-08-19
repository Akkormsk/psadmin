from django.contrib.auth import get_user_model
from django.db.models import Max
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ManagerSettings


@receiver(post_save, sender=get_user_model())
def create_manager_settings(sender, instance, created, **kwargs):
    if created and not instance.is_superuser:
        last_order = ManagerSettings.objects.aggregate(value=Max("sort_order"))["value"]
        ManagerSettings.objects.create(user=instance, sort_order=(last_order + 1) if last_order is not None else 0)
