from __future__ import annotations

import calendar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


class LeaveType(models.Model):
    team = models.ForeignKey("accounts.Team", on_delete=models.CASCADE, related_name="leave_types")
    name = models.CharField(max_length=100)
    # False: recorded but ignores floors, so it never blocks and is never blocked.
    counts_toward_floor = models.BooleanField(default=True)
    always_needs_approval = models.BooleanField(default=False)
    employee_bookable = models.BooleanField(default=True)
    # NULL = use the team's approval threshold.
    approval_threshold_override = models.PositiveSmallIntegerField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["team", "name"]
        constraints = [
            models.UniqueConstraint(fields=["team", "name"], name="uniq_leave_type_name_per_team")
        ]

    def __str__(self) -> str:
        return f"{self.team.name}: {self.name}"


class Holiday(models.Model):
    team = models.ForeignKey("accounts.Team", on_delete=models.CASCADE, related_name="holidays")
    date = models.DateField()
    name = models.CharField(max_length=150)

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["team", "date"], name="uniq_holiday_per_team_date")]

    def __str__(self) -> str:
        return f"{self.name} ({self.date})"


class Floor(models.Model):
    """Minimum available people for one resource role.

    Precedence for any day: date override (newest wins when overrides overlap),
    then the month floor, then no floor (unrestricted).
    """

    class Kind(models.TextChoices):
        MONTH = "month", "Month"
        OVERRIDE = "override", "Date Override"

    team = models.ForeignKey("accounts.Team", on_delete=models.CASCADE, related_name="floors")
    resource_role = models.ForeignKey("accounts.ResourceRole", on_delete=models.CASCADE, related_name="floors")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    start_date = models.DateField()
    end_date = models.DateField()
    min_available = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["team", "resource_role", "start_date", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(end_date__gte=F("start_date")), name="floor_end_not_before_start"),
            models.UniqueConstraint(
                fields=["team", "resource_role", "start_date"],
                condition=Q(kind="month"),
                name="uniq_month_floor_per_role",
            ),
        ]
        indexes = [models.Index(fields=["team", "resource_role", "start_date", "end_date"])]

    def __str__(self) -> str:
        return f"{self.resource_role} {self.kind} {self.start_date}..{self.end_date} = {self.min_available}"

    def clean(self) -> None:
        super().clean()
        if self.resource_role_id and self.team_id and self.resource_role.team_id != self.team_id:
            raise ValidationError("The resource role must belong to the team.")
        if self.kind == self.Kind.MONTH:
            last = calendar.monthrange(self.start_date.year, self.start_date.month)[1]
            if self.start_date.day != 1 or self.end_date != self.start_date.replace(day=last):
                raise ValidationError("A month floor must span the whole calendar month.")


class LeaveRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED_BY_EMPLOYEE = "cancelled_by_employee", "Cancelled by Employee"
        CANCELLED_BY_MANAGER = "cancelled_by_manager", "Cancelled by Manager"

    ACTIVE_STATUSES = (Status.PENDING, Status.APPROVED)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="leave_requests")
    team = models.ForeignKey("accounts.Team", on_delete=models.PROTECT, related_name="leave_requests")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="requests")
    start_date = models.DateField()
    end_date = models.DateField()
    working_days = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=24, choices=Status.choices)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.TextField(blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancel_reason = models.TextField(blank=True)
    floor_overridden = models.BooleanField(default=False)
    override_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-id"]
        indexes = [models.Index(fields=["team", "status"]), models.Index(fields=["user", "start_date"])]
        constraints = [
            models.CheckConstraint(condition=Q(end_date__gte=F("start_date")), name="leave_end_not_before_start")
        ]

    def __str__(self) -> str:
        return f"{self.user} {self.start_date}..{self.end_date} [{self.status}]"


class LeaveDay(models.Model):
    """One row per reserved working day.

    Rows exist only while a request is Pending or Approved; rejecting or cancelling
    a request deletes them, which releases the capacity. The request row keeps history.
    """

    request = models.ForeignKey(LeaveRequest, on_delete=models.CASCADE, related_name="days")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    team = models.ForeignKey("accounts.Team", on_delete=models.PROTECT, related_name="+")
    # Snapshot of the user's role at booking time, so a later role change does not
    # silently rewrite availability.
    resource_role = models.ForeignKey("accounts.ResourceRole", on_delete=models.PROTECT, related_name="+")
    date = models.DateField()
    counts_toward_floor = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["request", "date"], name="uniq_leave_day_per_request"),
            # A person cannot hold two active bookings on the same day (DB-level backstop).
            models.UniqueConstraint(fields=["user", "date"], name="uniq_leave_day_per_user"),
        ]
        indexes = [models.Index(fields=["team", "resource_role", "date"])]


class AuditLog(models.Model):
    """Append-only record of who did what."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    on_behalf_of = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # The team the change belongs to, so a manager sees only their own teams' history.
    # NULL for organisation-wide changes (regions, departments, manager accounts): administrators only.
    team = models.ForeignKey("accounts.Team", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=64)
    entity_type = models.CharField(max_length=64)
    entity_id = models.BigIntegerField(null=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["team", "-created_at"])]

    def save(self, *args, **kwargs) -> None:
        if self.pk is not None:
            raise ValidationError("Audit log entries cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit log entries cannot be deleted.")


class Notification(models.Model):
    """An email to send, written in the same transaction as the change that caused it.

    The row is created with the change (so a rolled-back change sends nothing) and the
    email goes out after the transaction commits. A failure is recorded here and retried
    by `manage.py send_notifications`; it never breaks the booking or decision itself.
    """

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    event = models.CharField(max_length=40)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    request = models.ForeignKey("leave.LeaveRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["sent_at", "attempts"])]
