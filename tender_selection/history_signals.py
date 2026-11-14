from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .history import sync_tenders
from .models import Tender


@receiver(pre_delete, sender=Tender, dispatch_uid="retain_tender_history_before_delete")
def retain_history(sender, instance, using, **kwargs):
    sync_tenders([instance.pk], using=using, detach=True)
