"""Team settings a manager (or administrator) can change: rules, roles, leave types, holidays."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from accounts.models import ResourceRole, Team, User

from .exceptions import InvalidRequest
from .models import Holiday, LeaveType
from .services import _audit, _lock_role, _require_manager

MAX_THRESHOLD = 365


def update_team_rules(
    *,
    actor: User,
    team: Team,
    approval_threshold: Optional[int],
    working_days: list[int],
    timezone: str,
    manager_counts_toward_headcount: bool,
) -> Team:
    """`approval_threshold`: None = nothing needs approval; 0 = everything does; N = more than N working days.

    Changing working days or the time zone does not move existing leave; days already booked stay as they are.
    """
    _require_manager(actor, team)
    if approval_threshold is not None and not 0 <= approval_threshold <= MAX_THRESHOLD:
        raise InvalidRequest(f"The approval threshold must be between 0 and {MAX_THRESHOLD} working days.")
    before = {
        "approval_threshold": team.approval_threshold,
        "working_days": team.working_days,
        "timezone": team.timezone,
        "manager_counts_toward_headcount": team.manager_counts_toward_headcount,
    }
    team.approval_threshold = approval_threshold
    team.working_days = sorted(set(working_days))
    team.timezone = timezone
    team.manager_counts_toward_headcount = manager_counts_toward_headcount
    try:
        team.clean()
    except ValidationError as exc:
        raise InvalidRequest(" ".join(m for msgs in exc.message_dict.values() for m in msgs)) from exc
    with transaction.atomic():
        team.save(update_fields=["approval_threshold", "working_days", "timezone", "manager_counts_toward_headcount"])
        _audit(actor=actor, action="team.settings", entity=team, before=before, after={
            "approval_threshold": team.approval_threshold,
            "working_days": team.working_days,
            "timezone": team.timezone,
            "manager_counts_toward_headcount": team.manager_counts_toward_headcount,
        })
    return team


# ---- holidays ---------------------------------------------------------------------------- #
def add_holiday(*, actor: User, team: Team, date: dt.date, name: str) -> Holiday:
    _require_manager(actor, team)
    name = name.strip()
    if not name:
        raise InvalidRequest("Give the holiday a name.")
    try:
        with transaction.atomic():
            holiday = Holiday.objects.create(team=team, date=date, name=name)
            _audit(actor=actor, action="holiday.add", entity=holiday, after={"date": date.isoformat(), "name": name})
    except IntegrityError as exc:
        raise InvalidRequest("There is already a holiday on that date.") from exc
    return holiday


def delete_holiday(*, actor: User, holiday: Holiday) -> None:
    _require_manager(actor, holiday.team)
    with transaction.atomic():
        _audit(actor=actor, action="holiday.delete", entity=holiday, before={"date": holiday.date.isoformat(), "name": holiday.name})
        holiday.delete()


# ---- leave types --------------------------------------------------------------------------- #
def save_leave_type(
    *,
    actor: User,
    team: Team,
    leave_type: Optional[LeaveType],
    name: str,
    counts_toward_floor: bool,
    always_needs_approval: bool,
    employee_bookable: bool,
    approval_threshold_override: Optional[int],
    active: bool,
) -> LeaveType:
    _require_manager(actor, team)
    name = name.strip()
    if not name:
        raise InvalidRequest("Give the leave type a name.")
    if approval_threshold_override is not None and not 0 <= approval_threshold_override <= MAX_THRESHOLD:
        raise InvalidRequest(f"The approval threshold must be between 0 and {MAX_THRESHOLD} working days.")
    if leave_type is not None and leave_type.team_id != team.id:
        raise InvalidRequest("That leave type belongs to another team.")
    fields = {
        "name": name,
        "counts_toward_floor": counts_toward_floor,
        "always_needs_approval": always_needs_approval,
        "employee_bookable": employee_bookable,
        "approval_threshold_override": approval_threshold_override,
        "active": active,
    }
    try:
        with transaction.atomic():
            if leave_type is None:
                leave_type = LeaveType.objects.create(team=team, **fields)
                _audit(actor=actor, action="leave_type.add", entity=leave_type, after=fields)
            else:
                before = {k: getattr(leave_type, k) for k in fields}
                for key, value in fields.items():
                    setattr(leave_type, key, value)
                leave_type.save()
                _audit(actor=actor, action="leave_type.update", entity=leave_type, before=before, after=fields)
    except IntegrityError as exc:
        raise InvalidRequest("This team already has a leave type with that name.") from exc
    return leave_type


# ---- resource roles -------------------------------------------------------------------------- #
def add_resource_role(*, actor: User, team: Team, name: str) -> ResourceRole:
    _require_manager(actor, team)
    name = name.strip()
    if not name:
        raise InvalidRequest("Give the role a name.")
    try:
        with transaction.atomic():
            role = ResourceRole.objects.create(team=team, name=name)
            _audit(actor=actor, action="role.add", entity=role, after={"name": name})
    except IntegrityError as exc:
        raise InvalidRequest("This team already has a role with that name.") from exc
    return role


def update_resource_role(*, actor: User, role: ResourceRole, name: str, active: bool) -> ResourceRole:
    """Rename a role or switch it off. A role with active members cannot be switched off."""
    _require_manager(actor, role.team)
    name = name.strip()
    if not name:
        raise InvalidRequest("Give the role a name.")
    try:
        with transaction.atomic():
            _lock_role(role.pk)
            role = ResourceRole.objects.select_related("team").get(pk=role.pk)
            if not active and role.active and User.objects.filter(resource_role=role, is_active=True).exists():
                raise InvalidRequest("This role still has active people. Move or deactivate them first.")
            before = {"name": role.name, "active": role.active}
            role.name, role.active = name, active
            role.save(update_fields=["name", "active"])
            _audit(actor=actor, action="role.update", entity=role, before=before, after={"name": name, "active": active})
    except IntegrityError as exc:
        raise InvalidRequest("This team already has a role with that name.") from exc
    return role
