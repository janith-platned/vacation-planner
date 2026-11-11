"""Business rules for floors, booking, approval and cancellation.

All state changes go through this module so the rules in the specification are
enforced in one place:

* A floor is the minimum number of people in one resource role who must be
  available on every working day. Precedence per day: date override (newest wins
  when overrides overlap), then the month floor, then none (unrestricted).
* A booking is accepted only if, on every working day in it, available people
  minus the requester is still at or above the floor. Pending and approved leave
  both reserve capacity.
* The check and the insert run in one transaction that holds a row lock on the
  requester's ResourceRole. All bookings for a role are serialised, so two people
  cannot both take the last slot. (Spec called for a lock per role and day; a
  role-level lock is simpler and safe at this scale.)
* Working days at or below the approval threshold are approved immediately;
  longer requests wait for the manager.
* Existing bookings are never cancelled automatically when a floor is raised.
  find_breaches() reports the days that fell below the floor.
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import ResourceRole, SystemRole, Team, User

from .exceptions import (
    FloorViolationError,
    InvalidRequest,
    InvalidState,
    LeaveError,
    NoWorkingDaysError,
    NotAllowed,
    OverlapError,
)
from .models import AuditLog, Floor, Holiday, LeaveDay, LeaveRequest, LeaveType

MAX_RANGE_DAYS = 366

Status = LeaveRequest.Status


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FloorViolation:
    date: dt.date
    floor: int
    available_after: int  # people available on that day if the booking went ahead


@dataclass(frozen=True)
class Breach:
    """A day on which a role is below its floor. Manager-only (it lists people)."""

    date: dt.date
    role_id: int
    role_name: str
    floor: int
    available: int
    user_ids: tuple[int, ...]


@dataclass(frozen=True)
class DayCount:
    """Anonymous per-day numbers for one role. Never contains names or ids."""

    date: dt.date
    headcount: int
    on_leave: int
    floor: Optional[int]
    slots_left: Optional[int]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def today_for(team: Team) -> dt.date:
    return dt.datetime.now(ZoneInfo(team.timezone)).date()


def is_team_manager(user: User, team: Team) -> bool:
    if user.system_role == SystemRole.ADMIN:
        return True
    return user.system_role == SystemRole.MANAGER and team.manager_id == user.id


def _require_manager(user: User, team: Team) -> None:
    if not is_team_manager(user, team):
        raise NotAllowed("Only the team's manager or an administrator can do this.")


def _audit(*, actor, action, entity, before=None, after=None, on_behalf_of=None) -> None:
    AuditLog.objects.create(
        actor=actor,
        on_behalf_of=on_behalf_of,
        action=action,
        entity_type=entity.__class__.__name__,
        entity_id=entity.pk,
        before=before,
        after=after,
    )


def _snapshot(request: LeaveRequest) -> dict:
    return {
        "status": request.status,
        "leave_type": request.leave_type_id,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "working_days": request.working_days,
    }


def working_days(team: Team, start: dt.date, end: dt.date) -> list[dt.date]:
    """Dates in [start, end] that are team working days and not team holidays."""
    if end < start:
        raise InvalidRequest("The end date is before the start date.")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise InvalidRequest(f"A date range cannot exceed {MAX_RANGE_DAYS} days.")
    holidays = set(Holiday.objects.filter(team=team, date__range=(start, end)).values_list("date", flat=True))
    weekdays = set(team.working_days)
    out = []
    day = start
    while day <= end:
        if day.weekday() in weekdays and day not in holidays:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# Floors and availability
# --------------------------------------------------------------------------- #
def effective_floors(team: Team, role: ResourceRole, dates: Iterable[dt.date]) -> dict[dt.date, Optional[int]]:
    dates = list(dates)
    if not dates:
        return {}
    lo, hi = min(dates), max(dates)
    base = Floor.objects.filter(team=team, resource_role=role, start_date__lte=hi, end_date__gte=lo)
    months = {f.start_date: f.min_available for f in base.filter(kind=Floor.Kind.MONTH)}
    overrides = list(base.filter(kind=Floor.Kind.OVERRIDE).order_by("id"))  # newest last, so it wins
    result: dict[dt.date, Optional[int]] = {}
    for day in dates:
        value = months.get(day.replace(day=1))
        for override in overrides:
            if override.start_date <= day <= override.end_date:
                value = override.min_available
        result[day] = value
    return result


def headcount(team: Team, role: ResourceRole) -> int:
    """Active people whose availability counts toward this role's floor."""
    who = Q(system_role=SystemRole.MEMBER)
    if team.manager_counts_toward_headcount:
        who |= Q(system_role=SystemRole.MANAGER)
    return User.objects.filter(is_active=True, team=team, resource_role=role).filter(who).count()


def booked_counts(
    team: Team, role: ResourceRole, dates: Iterable[dt.date], exclude_request: Optional[LeaveRequest] = None
) -> dict[dt.date, int]:
    qs = LeaveDay.objects.filter(team=team, resource_role=role, date__in=list(dates), counts_toward_floor=True)
    if exclude_request is not None:
        qs = qs.exclude(request=exclude_request)
    return dict(qs.order_by().values_list("date").annotate(n=Count("id")))


def check_floor(
    team: Team,
    role: ResourceRole,
    dates: Iterable[dt.date],
    exclude_request: Optional[LeaveRequest] = None,
) -> list[FloorViolation]:
    """Days on which one more person off would take the role below its floor."""
    dates = list(dates)
    floors = effective_floors(team, role, dates)
    head = headcount(team, role)
    booked = booked_counts(team, role, dates, exclude_request)
    violations = []
    for day in dates:
        floor = floors[day]
        if floor is None:
            continue
        available_after = head - booked.get(day, 0) - 1
        if available_after < floor:
            violations.append(FloorViolation(day, floor, available_after))
    return violations


def role_day_counts(team: Team, role: ResourceRole, start: dt.date, end: dt.date) -> list[DayCount]:
    """Anonymous counts per working day. This is the only data members get about colleagues."""
    dates = working_days(team, start, end)
    floors = effective_floors(team, role, dates)
    head = headcount(team, role)
    booked = booked_counts(team, role, dates)
    out = []
    for day in dates:
        on_leave = booked.get(day, 0)
        floor = floors[day]
        slots_left = None if floor is None else max(head - on_leave - floor, 0)
        out.append(DayCount(day, head, on_leave, floor, slots_left))
    return out


def find_breaches(team: Team, start: dt.date, end: dt.date, *, actor: User) -> list[Breach]:
    """Days below floor, with the people booked off. Manager or administrator only."""
    _require_manager(actor, team)
    dates = working_days(team, start, end)
    breaches = []
    for role in team.resource_roles.filter(active=True):
        floors = effective_floors(team, role, dates)
        head = headcount(team, role)
        booked = booked_counts(team, role, dates)
        for day in dates:
            floor = floors[day]
            if floor is None:
                continue
            available = head - booked.get(day, 0)
            if available < floor:
                user_ids = tuple(
                    LeaveDay.objects.filter(team=team, resource_role=role, date=day, counts_toward_floor=True)
                    .order_by("user_id")
                    .values_list("user_id", flat=True)
                )
                breaches.append(Breach(day, role.id, role.name, floor, available, user_ids))
    return breaches


@dataclass(frozen=True)
class CoverDay:
    """A day of a request on which the requester's role is below its floor right now."""

    date: dt.date
    floor: int
    available: int


def cover_issues(request: LeaveRequest) -> list[CoverDay]:
    """Days of an active request where the role is currently below its floor.

    A pending request already reserves its days, so this is what the manager is
    deciding on: a floor raised after the request was made shows up here.
    Manager-facing only. Requests whose leave type ignores floors have no issues.
    """
    rows = list(request.days.filter(counts_toward_floor=True).order_by("date").values_list("date", "resource_role_id"))
    if not rows:
        return []
    role = ResourceRole.objects.get(pk=rows[0][1])
    team = request.team
    dates = [d for d, _ in rows]
    floors = effective_floors(team, role, dates)
    head = headcount(team, role)
    booked = booked_counts(team, role, dates)
    issues = []
    for day in dates:
        floor = floors[day]
        available = head - booked.get(day, 0)
        if floor is not None and available < floor:
            issues.append(CoverDay(day, floor, available))
    return issues


def managed_teams(user: User):
    """Teams the user may manage: all for an administrator, their own for a manager."""
    if user.system_role == SystemRole.ADMIN:
        return Team.objects.select_related("region").all()
    if user.system_role == SystemRole.MANAGER:
        return Team.objects.select_related("region").filter(manager=user)
    return Team.objects.none()


# --------------------------------------------------------------------------- #
# Managing floors (manager or administrator)
# --------------------------------------------------------------------------- #
def set_month_floor(
    *, actor: User, team: Team, resource_role: ResourceRole, year: int, month: int, min_available: int
) -> Floor:
    """Set the floor for a whole month in one step. Existing overrides stay in place."""
    _require_manager(actor, team)
    if resource_role.team_id != team.id:
        raise InvalidRequest("The resource role does not belong to this team.")
    first = dt.date(year, month, 1)
    last = dt.date(year, month, calendar.monthrange(year, month)[1])
    with transaction.atomic():
        before = Floor.objects.filter(
            team=team, resource_role=resource_role, kind=Floor.Kind.MONTH, start_date=first
        ).first()
        floor, _ = Floor.objects.update_or_create(
            team=team,
            resource_role=resource_role,
            kind=Floor.Kind.MONTH,
            start_date=first,
            defaults={"end_date": last, "min_available": min_available},
        )
        _audit(
            actor=actor,
            action="floor.set_month",
            entity=floor,
            before={"min_available": before.min_available} if before else None,
            after={"min_available": min_available, "month": first.isoformat()[:7]},
        )
    return floor


def set_floor_override(
    *,
    actor: User,
    team: Team,
    resource_role: ResourceRole,
    start_date: dt.date,
    end_date: dt.date,
    min_available: int,
) -> Floor:
    """Override the floor for specific dates (a single day if start == end)."""
    _require_manager(actor, team)
    if resource_role.team_id != team.id:
        raise InvalidRequest("The resource role does not belong to this team.")
    if end_date < start_date:
        raise InvalidRequest("The end date is before the start date.")
    with transaction.atomic():
        floor = Floor.objects.create(
            team=team,
            resource_role=resource_role,
            kind=Floor.Kind.OVERRIDE,
            start_date=start_date,
            end_date=end_date,
            min_available=min_available,
        )
        _audit(
            actor=actor,
            action="floor.set_override",
            entity=floor,
            after={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "min_available": min_available,
            },
        )
    return floor


def delete_floor_override(*, actor: User, floor: Floor) -> None:
    """Remove an override; its dates fall back to the month floor."""
    _require_manager(actor, floor.team)
    if floor.kind != Floor.Kind.OVERRIDE:
        raise InvalidRequest("Only date overrides can be deleted; set the month floor instead.")
    with transaction.atomic():
        _audit(
            actor=actor,
            action="floor.delete_override",
            entity=floor,
            before={
                "start_date": floor.start_date.isoformat(),
                "end_date": floor.end_date.isoformat(),
                "min_available": floor.min_available,
            },
        )
        floor.delete()


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #
def _lock_role(role_id: int) -> ResourceRole:
    return ResourceRole.objects.select_for_update().get(pk=role_id)


def _validate_leave_type(leave_type: LeaveType, team: Team, privileged: bool) -> None:
    if leave_type.team_id != team.id or not leave_type.active:
        raise InvalidRequest("This leave type is not available for the team.")
    if not privileged and not leave_type.employee_bookable:
        raise NotAllowed("This leave type cannot be booked by employees.")


def _initial_status(team: Team, leave_type: LeaveType, days: int, privileged: bool) -> str:
    if privileged:  # a manager booking on someone's behalf is itself the approval
        return Status.APPROVED
    if leave_type.always_needs_approval:
        return Status.PENDING
    threshold = (
        leave_type.approval_threshold_override
        if leave_type.approval_threshold_override is not None
        else team.approval_threshold
    )
    if threshold is None:
        return Status.APPROVED
    return Status.PENDING if days > threshold else Status.APPROVED


def _apply_booking(
    *,
    request: Optional[LeaveRequest],
    user: User,
    role: ResourceRole,
    leave_type: LeaveType,
    start_date: dt.date,
    end_date: dt.date,
    actor: User,
    privileged: bool,
    override_floor: bool,
    override_reason: str,
) -> LeaveRequest:
    """Validate, then create (request=None) or update a request and its leave days."""
    team = user.team
    dates = working_days(team, start_date, end_date)
    if not dates:
        raise NoWorkingDaysError("The selected dates contain no working days.")

    clash = LeaveDay.objects.filter(user=user, date__in=dates)
    if request is not None:
        clash = clash.exclude(request=request)
    if clash.exists():
        raise OverlapError("There is already leave on one or more of these days.")

    counts = leave_type.counts_toward_floor and user.counts_toward_headcount()
    overridden = False
    if counts:
        violations = check_floor(team, role, dates, exclude_request=request)
        if violations:
            if override_floor and privileged and override_reason.strip():
                overridden = True
            else:
                raise FloorViolationError(violations)

    status = _initial_status(team, leave_type, len(dates), privileged)
    fields = {
        "leave_type": leave_type,
        "start_date": start_date,
        "end_date": end_date,
        "working_days": len(dates),
        "status": status,
        "floor_overridden": overridden,
        "override_reason": override_reason.strip() if overridden else "",
        "decided_by": actor if status == Status.APPROVED and privileged else None,
        "decided_at": timezone.now() if status == Status.APPROVED and privileged else None,
        "decision_comment": "",
    }
    if request is None:
        request = LeaveRequest.objects.create(user=user, team=team, created_by=actor, **fields)
    else:
        for key, value in fields.items():
            setattr(request, key, value)
        request.save()
    LeaveDay.objects.bulk_create(
        [
            LeaveDay(
                request=request, user=user, team=team, resource_role=role, date=day, counts_toward_floor=counts
            )
            for day in dates
        ]
    )
    return request


def book_leave(
    *,
    user: User,
    leave_type: LeaveType,
    start_date: dt.date,
    end_date: dt.date,
    actor: Optional[User] = None,
    override_floor: bool = False,
    override_reason: str = "",
    comment: str = "",
    today: Optional[dt.date] = None,
) -> LeaveRequest:
    """Book leave for `user`.

    `actor` defaults to the user. A manager or administrator can book for a team
    member (`actor` != `user`): the booking is approved at once, may be in the past,
    may use leave types that employees cannot book, may override the floor with a
    mandatory reason (flagged and audited), and can carry a `comment` for the employee.
    Raises FloorViolationError (with the failing dates) if the floor would be breached.
    """
    actor = actor or user
    privileged = actor.pk != user.pk
    if user.team_id is None or user.resource_role_id is None:
        raise InvalidRequest("The user has no team or resource role.")
    team = user.team
    if privileged:
        _require_manager(actor, team)
    elif not user.is_active:
        raise NotAllowed("This account is inactive.")
    _validate_leave_type(leave_type, team, privileged)
    if end_date < start_date:
        raise InvalidRequest("The end date is before the start date.")
    today = today or today_for(team)
    if start_date < today and not privileged:
        raise InvalidRequest("Leave cannot start in the past.")

    try:
        with transaction.atomic():
            role = _lock_role(user.resource_role_id)
            user = User.objects.select_related("team").get(pk=user.pk)
            if user.resource_role_id != role.id:
                raise InvalidRequest("The user's resource role changed; please retry.")
            request = _apply_booking(
                request=None,
                user=user,
                role=role,
                leave_type=leave_type,
                start_date=start_date,
                end_date=end_date,
                actor=actor,
                privileged=privileged,
                override_floor=override_floor,
                override_reason=override_reason,
            )
            if privileged and comment.strip():  # a note from the manager, shown to the employee
                request.decision_comment = comment.strip()
                request.save(update_fields=["decision_comment", "updated_at"])
            _audit(
                actor=actor,
                on_behalf_of=user if privileged else None,
                action="leave.book",
                entity=request,
                after={**_snapshot(request), "floor_overridden": request.floor_overridden},
            )
    except IntegrityError as exc:  # unique(user, date) backstop
        raise OverlapError("There is already leave on one or more of these days.") from exc
    return request


@dataclass(frozen=True)
class BookingPreview:
    working_days: int
    violations: tuple
    status: Optional[str]  # status the booking would get; None when it would be refused
    problem: Optional[str]  # reason for a non-floor refusal


def preview_booking(
    *,
    user: User,
    leave_type: LeaveType,
    start_date: dt.date,
    end_date: dt.date,
    today: Optional[dt.date] = None,
    privileged: bool = False,
) -> BookingPreview:
    """Read-only look at what book_leave would do for `user`.

    With `privileged=True` it previews a manager booking for the employee: past
    dates and employee-restricted leave types are allowed and the result is approved.
    Advisory only: it takes no lock, so book_leave re-checks at submit time.
    """
    try:
        if user.team_id is None or user.resource_role_id is None:
            raise InvalidRequest("The account has no team or resource role.")
        team = user.team
        _validate_leave_type(leave_type, team, privileged=privileged)
        if end_date < start_date:
            raise InvalidRequest("The end date is before the start date.")
        if start_date < (today or today_for(team)) and not privileged:
            raise InvalidRequest("Leave cannot start in the past.")
        dates = working_days(team, start_date, end_date)
        if not dates:
            raise NoWorkingDaysError("The selected dates contain no working days.")
        if LeaveDay.objects.filter(user=user, date__in=dates).exists():
            raise OverlapError("There is already leave on one or more of these days.")
    except LeaveError as exc:
        return BookingPreview(0, (), None, str(exc))
    violations: tuple = ()
    if leave_type.counts_toward_floor and user.counts_toward_headcount():
        violations = tuple(check_floor(team, user.resource_role, dates))
    status = None if violations else _initial_status(team, leave_type, len(dates), privileged=privileged)
    return BookingPreview(len(dates), violations, status, None)


def modify_leave(
    *,
    request: LeaveRequest,
    actor: User,
    start_date: dt.date,
    end_date: dt.date,
    leave_type: Optional[LeaveType] = None,
    override_floor: bool = False,
    override_reason: str = "",
    comment: str = "",
    today: Optional[dt.date] = None,
) -> LeaveRequest:
    """Change the dates (or type) of an active request.

    The change is validated as a new request with the old days released, then
    replaces the original atomically. If it fails, nothing changes. When the owner
    edits, approval restarts. When a manager edits, the result is approved at once,
    may override the floor with a reason, and can carry a `comment` for the employee.
    """
    team = request.team
    is_owner = actor.pk == request.user_id
    if not is_owner:
        _require_manager(actor, team)
    privileged = not is_owner
    today = today or today_for(team)
    if end_date < start_date:
        raise InvalidRequest("The end date is before the start date.")
    if start_date < today and not privileged:
        raise InvalidRequest("Leave cannot start in the past.")

    try:
        with transaction.atomic():
            user = User.objects.select_related("team").get(pk=request.user_id)
            role = _lock_role(user.resource_role_id)
            locked = LeaveRequest.objects.select_for_update().get(pk=request.pk)
            if locked.status not in LeaveRequest.ACTIVE_STATUSES:
                raise InvalidState("Only pending or approved requests can be changed.")
            if locked.end_date < today:
                raise InvalidState("Leave that has already ended cannot be changed.")
            new_type = leave_type or locked.leave_type
            _validate_leave_type(new_type, team, privileged)
            before = _snapshot(locked)
            locked.days.all().delete()  # released first; any failure below rolls this back
            updated = _apply_booking(
                request=locked,
                user=user,
                role=role,
                leave_type=new_type,
                start_date=start_date,
                end_date=end_date,
                actor=actor,
                privileged=privileged,
                override_floor=override_floor,
                override_reason=override_reason,
            )
            if privileged and comment.strip():  # a note from the manager, shown to the employee
                updated.decision_comment = comment.strip()
                updated.save(update_fields=["decision_comment", "updated_at"])
            _audit(
                actor=actor,
                on_behalf_of=user if privileged else None,
                action="leave.modify",
                entity=updated,
                before=before,
                after={**_snapshot(updated), "floor_overridden": updated.floor_overridden, "comment": updated.decision_comment},
            )
    except IntegrityError as exc:
        raise OverlapError("You already have leave on one or more of these days.") from exc
    return updated


# --------------------------------------------------------------------------- #
# Approval and cancellation
# --------------------------------------------------------------------------- #
def decide_request(*, request: LeaveRequest, actor: User, approve: bool, comment: str = "") -> LeaveRequest:
    """Approve or reject a pending request. Rejecting releases its days."""
    _require_manager(actor, request.team)
    with transaction.atomic():
        locked = LeaveRequest.objects.select_for_update().get(pk=request.pk)
        if locked.status != Status.PENDING:
            raise InvalidState("Only pending requests can be approved or rejected.")
        before = _snapshot(locked)
        locked.status = Status.APPROVED if approve else Status.REJECTED
        locked.decided_by = actor
        locked.decided_at = timezone.now()
        locked.decision_comment = comment
        locked.save()
        if not approve:
            locked.days.all().delete()
        _audit(
            actor=actor,
            action="leave.approve" if approve else "leave.reject",
            entity=locked,
            before=before,
            after=_snapshot(locked),
        )
    return locked


def revoke_approved_leave(
    *, request: LeaveRequest, actor: User, reason: str, today: Optional[dt.date] = None
) -> LeaveRequest:
    """A manager rejects leave that was already approved. The days are released.

    The employee was told yes, so a reason is required and is shown to them.
    Leave that has already ended is history and cannot be rejected.
    """
    _require_manager(actor, request.team)
    if not reason.strip():
        raise InvalidRequest("A reason is required to reject approved leave.")
    today = today or today_for(request.team)
    with transaction.atomic():
        locked = LeaveRequest.objects.select_for_update().get(pk=request.pk)
        if locked.status != Status.APPROVED:
            raise InvalidState("Only approved leave can be rejected here.")
        if locked.end_date < today:
            raise InvalidState("Leave that has already ended cannot be rejected.")
        before = _snapshot(locked)
        locked.status = Status.REJECTED
        locked.decided_by = actor
        locked.decided_at = timezone.now()
        locked.decision_comment = reason.strip()
        locked.save()
        locked.days.all().delete()
        _audit(actor=actor, action="leave.revoke", entity=locked, before=before, after=_snapshot(locked))
    return locked


def cancel_leave(
    *, request: LeaveRequest, actor: User, reason: str = "", today: Optional[dt.date] = None
) -> LeaveRequest:
    """Cancel an active request and release its days.

    Employees cancel their own bookings freely. A manager cancelling someone else's
    booking must give a reason. Leave that has already ended cannot be cancelled.
    """
    team = request.team
    is_owner = actor.pk == request.user_id
    if not is_owner:
        _require_manager(actor, team)
        if not reason.strip():
            raise InvalidRequest("A reason is required when cancelling someone else's leave.")
    today = today or today_for(team)
    with transaction.atomic():
        locked = LeaveRequest.objects.select_for_update().get(pk=request.pk)
        if locked.status not in LeaveRequest.ACTIVE_STATUSES:
            raise InvalidState("This request is not active.")
        if locked.end_date < today:
            raise InvalidState("Leave that has already ended cannot be cancelled.")
        before = _snapshot(locked)
        locked.status = Status.CANCELLED_BY_EMPLOYEE if is_owner else Status.CANCELLED_BY_MANAGER
        locked.cancelled_by = actor
        locked.cancel_reason = reason.strip()
        locked.save()
        locked.days.all().delete()
        _audit(actor=actor, action="leave.cancel", entity=locked, before=before, after=_snapshot(locked))
    return locked
