"""Settings screen: team rules, resource roles, leave types and holidays."""
from __future__ import annotations

import calendar
import datetime as dt
from urllib.parse import urlencode
from zoneinfo import available_timezones

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.models import ResourceRole

from . import services, settings_services as svc
from .exceptions import LeaveError
from .manage_pages import _day, _require_manager_role
from .models import Holiday, LeaveType

_TIMEZONES = sorted(available_timezones())


def _int_or_none(raw: str):
    raw = (raw or "").strip()
    if raw == "":
        return None
    return int(raw)  # ValueError handled by callers


def _team(request, raw):
    teams = list(services.managed_teams(request.user))
    chosen = next((t for t in teams if str(t.pk) == str(raw)), None)
    return teams, chosen or (teams[0] if teams else None)


def _back(team):
    return redirect(f"{reverse('settings')}?{urlencode({'team': team.pk})}")


def _run(request, team, action, success: str):
    """Run a settings change, turning rule violations into messages."""
    try:
        action()
    except LeaveError as exc:
        messages.error(request, str(exc))
    except ValueError:
        messages.error(request, "Enter whole numbers only.")
    else:
        messages.success(request, success)
    return _back(team)


@login_required
def settings_page(request):
    _require_manager_role(request.user)
    teams, team = _team(request, request.GET.get("team"))
    if team is None:
        return render(request, "leave/settings.html", {"team": None})
    year = request.GET.get("year", "")
    year = int(year) if year.isdigit() and 2000 <= int(year) <= 2100 else services.today_for(team).year
    return render(
        request,
        "leave/settings.html",
        {
            "team": team,
            "teams": teams,
            "weekdays": [(i, calendar.day_name[i]) for i in range(7)],
            "timezones": _TIMEZONES if team.timezone in _TIMEZONES else [team.timezone, *_TIMEZONES],
            "roles": team.resource_roles.order_by("-active", "name"),
            "leave_types": team.leave_types.order_by("-active", "name"),
            "holidays": team.holidays.filter(date__year=year),
            "year": year,
            "never_approve": team.approval_threshold is None,
        },
    )


@login_required
@require_POST
def save_rules_view(request):
    _require_manager_role(request.user)
    _, team = _team(request, request.POST.get("team"))
    if team is None:
        return redirect("settings")

    def action():
        threshold = None if request.POST.get("approval_mode") == "never" else int(request.POST.get("approval_threshold", ""))
        svc.update_team_rules(
            actor=request.user,
            team=team,
            approval_threshold=threshold,
            working_days=[int(d) for d in request.POST.getlist("working_days")],
            timezone=request.POST.get("timezone", team.timezone),
            manager_counts_toward_headcount=request.POST.get("manager_counts") == "1",
            notify_manager_new_request=request.POST.get("notify_manager") == "1",
            notify_employee_updates=request.POST.get("notify_employee") == "1",
        )

    return _run(request, team, action, "Team rules saved. Leave that is already booked is not changed.")


@login_required
@require_POST
def add_role_view(request):
    _require_manager_role(request.user)
    _, team = _team(request, request.POST.get("team"))
    if team is None:
        return redirect("settings")
    name = request.POST.get("name", "")
    return _run(request, team, lambda: svc.add_resource_role(actor=request.user, team=team, name=name),
                f"Role added. Set its floor on the Floors screen.")


@login_required
@require_POST
def update_role_view(request, pk: int):
    _require_manager_role(request.user)
    role = get_object_or_404(ResourceRole.objects.select_related("team"), pk=pk, team__in=services.managed_teams(request.user))
    return _run(
        request,
        role.team,
        lambda: svc.update_resource_role(
            actor=request.user, role=role, name=request.POST.get("name", ""), active=request.POST.get("active") == "1"
        ),
        "Role saved.",
    )


@login_required
@require_POST
def save_leave_type_view(request):
    """Create (no `id`) or update a leave type."""
    _require_manager_role(request.user)
    _, team = _team(request, request.POST.get("team"))
    if team is None:
        return redirect("settings")
    raw_id = request.POST.get("id", "")
    leave_type = get_object_or_404(LeaveType, pk=raw_id, team=team) if raw_id.isdigit() else None
    p = request.POST
    return _run(
        request,
        team,
        lambda: svc.save_leave_type(
            actor=request.user,
            team=team,
            leave_type=leave_type,
            name=p.get("name", ""),
            counts_toward_floor=p.get("counts_toward_floor") == "1",
            always_needs_approval=p.get("always_needs_approval") == "1",
            employee_bookable=p.get("employee_bookable") == "1",
            approval_threshold_override=_int_or_none(p.get("approval_threshold_override", "")),
            active=p.get("active") == "1" if leave_type else True,
        ),
        "Leave type saved.",
    )


@login_required
@require_POST
def add_holiday_view(request):
    _require_manager_role(request.user)
    _, team = _team(request, request.POST.get("team"))
    if team is None:
        return redirect("settings")
    try:
        date = dt.date.fromisoformat(request.POST.get("date", ""))
    except ValueError:
        messages.error(request, "Enter a valid date.")
        return _back(team)
    return _run(
        request, team,
        lambda: svc.add_holiday(actor=request.user, team=team, date=date, name=request.POST.get("name", "")),
        f"Holiday added on {_day(date)}. It is no longer a working day for this team.",
    )


@login_required
@require_POST
def delete_holiday_view(request, pk: int):
    _require_manager_role(request.user)
    holiday = get_object_or_404(Holiday.objects.select_related("team"), pk=pk, team__in=services.managed_teams(request.user))
    return _run(request, holiday.team, lambda: svc.delete_holiday(actor=request.user, holiday=holiday), "Holiday removed.")
