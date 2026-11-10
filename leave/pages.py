"""Server-rendered screens.

Team members only ever see anonymous counts for their own resource role plus
their own bookings. Nothing here renders another person's name.
"""
from __future__ import annotations

import calendar
import datetime as dt

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import SystemRole, Team

from . import services
from .calendar_grid import build_month_grid
from .exceptions import FloorViolationError, LeaveError
from .forms import BookLeaveForm
from .models import LeaveDay, LeaveRequest

Status = LeaveRequest.Status


def _managed_teams(user):
    if user.system_role == SystemRole.ADMIN:
        return Team.objects.select_related("region").all()
    if user.system_role == SystemRole.MANAGER:
        return user.managed_teams.select_related("region").all()
    return Team.objects.none()


def _parse_month(value: str | None, fallback: dt.date) -> tuple[int, int]:
    try:
        parsed = dt.datetime.strptime(value or "", "%Y-%m")
        return parsed.year, parsed.month
    except ValueError:
        return fallback.year, fallback.month


def _shift_month(year: int, month: int, delta: int) -> str:
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


@login_required
def calendar_page(request):
    user = request.user
    managed = list(_managed_teams(user))
    team_param = request.GET.get("team")

    if team_param:
        try:
            team = next((t for t in managed if t.pk == int(team_param)), None)
        except ValueError:
            return HttpResponseBadRequest("invalid team")
        if team is None:
            raise PermissionDenied
        manager_view = True
    elif managed:
        team, manager_view = managed[0], True
    elif user.team_id and user.resource_role_id:
        team, manager_view = user.team, False
    else:
        return render(request, "leave/calendar.html", {"no_team": True})

    if manager_view:
        roles = list(team.resource_roles.filter(active=True))
        role = next((r for r in roles if str(r.pk) == request.GET.get("role")), roles[0] if roles else None)
    else:
        role, roles = user.resource_role, [user.resource_role]

    today = services.today_for(team)
    year, month = _parse_month(request.GET.get("month"), today)
    first = dt.date(year, month, 1)

    # Range selection books leave for the signed-in user, so it is only offered
    # when they are looking at their own team and their own resource role.
    can_book = bool(role is not None and user.team_id == team.id and user.resource_role_id == role.id)
    leave_types = (
        list(team.leave_types.filter(active=True, employee_bookable=True)) if can_book else []
    )
    last = dt.date(year, month, calendar.monthrange(year, month)[1])

    weeks = []
    if role is not None:
        counts = services.role_day_counts(team, role, first, last)
        mine = set(LeaveDay.objects.filter(user=user, date__range=(first, last)).values_list("date", flat=True))
        weeks = build_month_grid(year, month, counts, mine)

    return render(
        request,
        "leave/calendar.html",
        {
            "team": team,
            "teams": managed,
            "manager_view": manager_view,
            "roles": roles,
            "role": role,
            "weeks": weeks,
            "month_label": first.strftime("%B %Y"),
            "month": f"{year:04d}-{month:02d}",
            "prev_month": _shift_month(year, month, -1),
            "next_month": _shift_month(year, month, 1),
            "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "today": today,
            "can_book": can_book,
            "leave_types": leave_types,
        },
    )


@login_required
def book_page(request):
    user = request.user
    if not (user.team_id and user.resource_role_id):
        messages.error(request, "Your account has no team or resource role yet. Ask your manager to set it up.")
        return redirect("calendar")

    form = BookLeaveForm(request.POST or None, user=user)
    violations = []
    if request.method == "POST" and form.is_valid():
        try:
            booking = services.book_leave(
                user=user,
                leave_type=form.cleaned_data["leave_type"],
                start_date=form.cleaned_data["start_date"],
                end_date=form.cleaned_data["end_date"],
            )
        except FloorViolationError as exc:
            violations = exc.violations
        except LeaveError as exc:
            form.add_error(None, str(exc))
        else:
            if booking.status == Status.PENDING:
                messages.success(request, "Request submitted. It needs your manager's approval.")
            else:
                messages.success(request, "Leave booked and approved.")
            return redirect("my-leave")
    return render(request, "leave/book.html", {"form": form, "violations": violations})


@login_required
def my_leave_page(request):
    user = request.user
    today = services.today_for(user.team) if user.team_id else dt.date.today()
    rows = [
        {
            "request": r,
            "can_cancel": r.status in LeaveRequest.ACTIVE_STATUSES and r.end_date >= today,
        }
        for r in user.leave_requests.select_related("leave_type").order_by("-start_date", "-id")
    ]
    return render(request, "leave/my_leave.html", {"rows": rows})


@login_required
@require_POST
def cancel_leave_view(request, pk: int):
    leave_request = get_object_or_404(LeaveRequest, pk=pk, user=request.user)
    try:
        services.cancel_leave(request=leave_request, actor=request.user)
    except LeaveError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Leave cancelled. Those days are available to your team again.")
    return redirect("my-leave")
