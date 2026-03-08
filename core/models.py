from django.conf import settings
from django.db import models


class Profile(models.Model):
    ROLE_MANAGER = "manager"
    ROLE_OWNER = "owner"

    ROLE_CHOICES = [
        (ROLE_MANAGER, "Manager"),
        (ROLE_OWNER, "Owner"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=ROLE_MANAGER,
    )

    def __str__(self):
        return f"{self.user.username} ({self.role})"