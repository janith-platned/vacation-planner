from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.core.exceptions import ValidationError
from django.db import models


def default_working_days() -> list[int]:
    """Monday to Friday (Python weekday numbers: Monday = 0)."""
    return [0, 1, 2, 3, 4]


class SystemRole(models.TextChoices):
    MEMBER = "member", "Team member"
    MANAGER = "manager", "Manager"
    ADMIN = "admin", "Administrator"


class Region(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Department(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Team(models.Model):
    """One manager and one region per team. A manager can manage several teams."""

    name = models.CharField(max_length=150, unique=True)
    manager = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="managed_teams"
    )
    region = models.ForeignKey(Region, on_delete=models.PROTECT, related_name="teams")
    # Working days above which a request needs approval. 0 = every request needs
    # approval; NULL = no request ever needs approval.
    approval_threshold = models.PositiveSmallIntegerField(null=True, blank=True, default=5)
    working_days = models.JSONField(default=default_working_days)
    timezone = models.CharField(max_length=64, default="America/New_York")
    manager_counts_toward_headcount = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        days = self.working_days
        if (
            not isinstance(days, list)
            or not days
            or len(set(days)) != len(days)
            or any(not isinstance(d, int) or d < 0 or d > 6 for d in days)
        ):
            raise ValidationError({"working_days": "Use a non-empty list of unique weekday numbers 0-6."})
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError({"timezone": "Unknown time zone."})
        if self.manager_id and self.manager.system_role not in (SystemRole.MANAGER, SystemRole.ADMIN):
            raise ValidationError({"manager": "The team manager must have the manager or administrator role."})


class ResourceRole(models.Model):
    """A resource role within a team, such as Functional, Technical or Systems."""

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="resource_roles")
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["team", "name"]
        constraints = [
            models.UniqueConstraint(fields=["team", "name"], name="uniq_resource_role_name_per_team")
        ]

    def __str__(self) -> str:
        return f"{self.team.name}: {self.name}"


class UserManager(DjangoUserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        # A superuser created from the command line is the application's administrator.
        extra_fields.setdefault("system_role", SystemRole.ADMIN)
        extra_fields.setdefault("must_change_password", False)
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    objects = UserManager()

    system_role = models.CharField(
        max_length=10, choices=SystemRole.choices, default=SystemRole.MEMBER
    )
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.PROTECT, related_name="members"
    )
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="users"
    )
    resource_role = models.ForeignKey(
        ResourceRole, null=True, blank=True, on_delete=models.SET_NULL, related_name="users"
    )
    must_change_password = models.BooleanField(default=True)

    def clean(self) -> None:
        super().clean()
        if self.resource_role_id and self.team_id and self.resource_role.team_id != self.team_id:
            raise ValidationError({"resource_role": "The resource role must belong to the user's team."})
        if self.system_role == SystemRole.MEMBER and self.is_active:
            if not self.team_id or not self.resource_role_id:
                raise ValidationError("Team members need a team and a resource role.")

    def counts_toward_headcount(self) -> bool:
        """Whether this user's availability is part of their role's floor calculation."""
        if not (self.is_active and self.team_id and self.resource_role_id):
            return False
        if self.system_role == SystemRole.MEMBER:
            return True
        if self.system_role == SystemRole.MANAGER:
            return self.team.manager_counts_toward_headcount
        return False
