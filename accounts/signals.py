from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ResourceRole, Team

DEFAULT_RESOURCE_ROLES = ("Functional", "Technical", "Systems")


@receiver(post_save, sender=Team)
def create_default_resource_roles(sender, instance: Team, created: bool, **kwargs) -> None:
    if created:
        ResourceRole.objects.bulk_create(
            [ResourceRole(team=instance, name=name) for name in DEFAULT_RESOURCE_ROLES]
        )
