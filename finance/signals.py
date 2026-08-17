from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ManagerSettings


@receiver(post_save, sender=get_user_model())
def create_manager_settings(sender, instance, created, **kwargs):
    if created and not instance.is_superuser:
        ManagerSettings.objects.create(user=instance)
