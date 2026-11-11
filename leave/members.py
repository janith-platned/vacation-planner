"""Team member management by a team's manager (or an administrator).

Everything here changes who counts toward a role's floor, so each change takes
the same row lock on the resource role that booking uses.
"""
from __future__ import annotations

import datetime as dt
import secrets
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from accounts.models import ResourceRole, SystemRole, User

from .exceptions import InvalidRequest
from .models import LeaveDay, LeaveRequest
from .services import _audit, _lock_role, _require_manager, _snapshot, cancel_leave, today_for

_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters


def generate_temp_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(12))


def _require_member(user: User) -> None:
    if user.system_role != SystemRole.MEMBER or user.team_id is None:
        raise InvalidRequest("Only team members can be managed here.")


def add_member(
    *, actor: User, team, resource_role: ResourceRole, username: str, first_name: str, last_name: str, email: str = ""
) -> tuple[User, str]:
    """Create a team member with a temporary password they must change at first sign-in."""
    _require_manager(actor, team)
    if resource_role.team_id != team.id or not resource_role.active:
        raise InvalidRequest("Choose one of this team's active resource roles.")
    username = username.strip()
    if not username or not first_name.strip():
        raise InvalidRequest("A username and a first name are required.")
    password = generate_temp_password()
    try:
        with transaction.atomic():
            _lock_role(resource_role.pk)
            user = User(
                username=username,
                first_name=first_name.strip(),
                last_name=last_name.strip(),
                email=email.strip(),
                system_role=SystemRole.MEMBER,
                team=team,
                resource_role=resource_role,
                must_change_password=True,
            )
            user.full_clean(exclude=["password"])
            user.set_password(password)
            user.save()
            _audit(actor=actor, action="member.add", entity=user, after={"team": team.id, "role": resource_role.id})
    except ValidationError as exc:
        raise InvalidRequest(" ".join(m for msgs in exc.message_dict.values() for m in msgs)) from exc
    except IntegrityError as exc:
        raise InvalidRequest("That username is already taken.") from exc
    return user, password


def update_member(
    *, actor: User, user: User, first_name: str, last_name: str, email: str, resource_role: ResourceRole,
    today: Optional[dt.date] = None,
) -> User:
    """Change a member's details. Moving them to another role needs them to have no leave from today on,
    because their booked days count against the role they were booked under."""
    _require_member(user)
    _require_manager(actor, user.team)
    if resource_role.team_id != user.team_id or not resource_role.active:
        raise InvalidRequest("Choose one of this team's active resource roles.")
    if not first_name.strip():
        raise InvalidRequest("A first name is required.")
    today = today or today_for(user.team)
    with transaction.atomic():
        old_role_id = user.resource_role_id
        for role_id in sorted({old_role_id, resource_role.pk}):  # fixed order avoids deadlocks
            _lock_role(role_id)
        user = User.objects.select_related("team").get(pk=user.pk)
        if resource_role.pk != user.resource_role_id:
            if LeaveDay.objects.filter(user=user, date__gte=today).exists():
                raise InvalidRequest(
                    "This person has leave from today onward. Cancel it first, then change their role "
                    "(booked days count against the role they were booked under)."
                )
        before = {"role": user.resource_role_id, "name": user.get_full_name(), "email": user.email}
        user.first_name, user.last_name, user.email = first_name.strip(), last_name.strip(), email.strip()
        user.resource_role = resource_role
        user.save(update_fields=["first_name", "last_name", "email", "resource_role"])
        _audit(
            actor=actor,
            action="member.update",
            entity=user,
            before=before,
            after={"role": resource_role.id, "name": user.get_full_name(), "email": user.email},
        )
    return user


def deactivate_member(*, actor: User, user: User, today: Optional[dt.date] = None) -> dict:
    """Deactivate a member and release their leave from today on.

    Leave that has not started is cancelled. Leave in progress is cut back to the days already
    taken. Past leave is untouched. Returns counts for the confirmation message.
    """
    _require_member(user)
    _require_manager(actor, user.team)
    today = today or today_for(user.team)
    cancelled = trimmed = 0
    with transaction.atomic():
        _lock_role(user.resource_role_id)
        user = User.objects.select_related("team").get(pk=user.pk)
        if not user.is_active:
            raise InvalidRequest("This account is already inactive.")
        live = LeaveRequest.objects.select_for_update().filter(
            user=user, status__in=LeaveRequest.ACTIVE_STATUSES, end_date__gte=today
        )
        for leave_request in live:
            taken = list(leave_request.days.filter(date__lt=today).order_by("date").values_list("date", flat=True))
            if not taken:
                cancel_leave(request=leave_request, actor=actor, reason="Account deactivated.", today=today)
                cancelled += 1
                continue
            before = _snapshot(leave_request)
            leave_request.days.filter(date__gte=today).delete()
            leave_request.end_date = taken[-1]
            leave_request.working_days = len(taken)
            leave_request.save(update_fields=["end_date", "working_days", "updated_at"])
            _audit(actor=actor, action="leave.truncate", entity=leave_request, before=before, after=_snapshot(leave_request))
            trimmed += 1
        user.is_active = False
        user.save(update_fields=["is_active"])
        _audit(
            actor=actor,
            action="member.deactivate",
            entity=user,
            after={"cancelled": cancelled, "trimmed": trimmed},
        )
    return {"cancelled": cancelled, "trimmed": trimmed}


def reactivate_member(*, actor: User, user: User) -> User:
    _require_member(user)
    _require_manager(actor, user.team)
    if user.resource_role_id is None or not user.resource_role.active:
        raise InvalidRequest("Give this person an active resource role first.")
    with transaction.atomic():
        _lock_role(user.resource_role_id)
        user.is_active = True
        user.save(update_fields=["is_active"])
        _audit(actor=actor, action="member.reactivate", entity=user)
    return user


def reset_password(*, actor: User, user: User) -> str:
    """Set a new temporary password. The member must change it at next sign-in."""
    _require_member(user)
    _require_manager(actor, user.team)
    password = generate_temp_password()
    user.set_password(password)
    user.must_change_password = True
    user.save(update_fields=["password", "must_change_password"])
    _audit(actor=actor, action="member.reset_password", entity=user)
    return password
