"""Days where a role is below its floor, and who is booked off on them.

Raising a floor never cancels leave, so this is where a manager sees what fell
short and decides, person by person, what to do.
"""
from __future__ import annotations

import datetime as dt

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_POST

from . import services
from .exceptions import LeaveError
from .manage_pages import _back, _dates, _managed_request, _person, _require_manager_role
from .models import LeaveDay

WINDOWS = [(30, "Next 30 days"), (60, "Next 60 days"), (90, "Next 90 days"), (180, "Next 180 days")]


def _window(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 60
    return value if value in dict(WINDOWS) else 60


def build_breach_days(user, teams, days: int, role: str = "") -> list[dict]:
    """Breached days from today for the given teams, oldest first, with who is booked off."""
    out = []
    for team in teams:
        today = services.today_for(team)
        for breach in services.find_breaches(team, today, today + dt.timedelta(days=days - 1), actor=user):
            if role and breach.role_name != role:
                continue
            people = []
            leave_days = (
                LeaveDay.objects.filter(
                    team=team, resource_role_id=breach.role_id, date=breach.date, counts_toward_floor=True
                )
                .select_related("request", "request__leave_type", "user")
                .order_by("user__first_name", "user__last_name", "user_id")
            )
            for leave_day in leave_days:
                people.append({"user": leave_day.user, "request": leave_day.request})
            out.append(
                {
                    "team": team,
                    "date": breach.date,
                    "role": breach.role_name,
                    "floor": breach.floor,
                    "available": breach.available,
                    "short_by": breach.floor - breach.available,
                    "people": people,
                }
            )
    out.sort(key=lambda b: (b["date"], b["team"].name, b["role"]))
    return out


@login_required
def breaches_page(request):
    _require_manager_role(request.user)
    teams = list(services.managed_teams(request.user))
    selected = request.GET.get("team", "")
    scope = [t for t in teams if str(t.pk) == selected] if selected else teams
    days = _window(request.GET.get("days", ""))
    role = request.GET.get("role", "")
    rows = build_breach_days(request.user, scope, days, role)
    role_names = sorted({r.name for t in scope for r in t.resource_roles.filter(active=True)})
    return render(
        request,
        "leave/breaches.html",
        {
            "teams": teams,
            "selected_team": selected,
            "days": days,
            "windows": WINDOWS,
            "role": role,
            "roles": role_names,
            "rows": rows,
            "multi_team": len(scope) > 1,
        },
    )


@login_required
@require_POST
def cancel_request_view(request, pk: int):
    """A manager cancels someone's leave (for example after a floor was raised). A reason is required."""
    _require_manager_role(request.user)
    leave_request = _managed_request(request, pk)
    try:
        services.cancel_leave(
            request=leave_request, actor=request.user, reason=request.POST.get("reason", "").strip()[:500]
        )
    except LeaveError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Cancelled {_person(leave_request.user)}'s leave ({_dates(leave_request)}). The days are released "
            "and the employee sees your reason.",
        )
    return _back(request, default="breaches")
