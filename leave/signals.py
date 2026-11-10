from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import Team

from .models import LeaveType


@receiver(post_save, sender=Team)
def create_default_leave_type(sender, instance: Team, created: bool, **kwargs) -> None:
    if created:
        LeaveType.objects.create(team=instance, name="Vacation")
