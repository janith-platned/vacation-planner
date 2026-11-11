"""Manager screen for floors: set a month in one step, override specific dates."""
from __future__ import annotations

import calendar
import datetime as dt
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import services
from .exceptions import LeaveError
from .forms import OverrideFloorForm
from .manage_pages import _day, _require_manager_role
from .models import Floor

MAX_FLOOR = 999


def _month(raw: str, default: dt.date) -> dt.date:
    try:
        year, month = raw.split("-")
        return dt.date(int(year), int(month), 1)
    except (ValueError, AttributeError):
        return default.replace(day=1)


def _shift(first: dt.date, months: int) -> dt.date:
    index = first.year * 12 + first.month - 1 + months
    return dt.date(index // 12, index % 12 + 1, 1)


def _team(request, raw):
    teams = list(services.managed_teams(request.user))
    chosen = next((t for t in teams if str(t.pk) == str(raw)), None)
    return teams, chosen or (teams[0] if teams else None)


def _redirect(team, first):
    return redirect(f"{reverse('floors')}?{urlencode({'team': team.pk, 'month': first.strftime('%Y-%m')})}")


def _cell_state(count) -> str:
    if count.floor is None:
        return "nofloor"
    if count.headcount - count.on_leave < count.floor:
        return "breach"
    if count.slots_left == 0:
        return "low"
    return "ok"


@login_required
def floors_page(request):
    _require_manager_role(request.user)
    teams, team = _team(request, request.GET.get("team"))
    if team is None:
        return render(request, "leave/floors.html", {"team": None})

    first = _month(request.GET.get("month", ""), services.today_for(team))
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    overrides = list(
        Floor.objects.filter(team=team, kind=Floor.Kind.OVERRIDE, start_date__lte=last, end_date__gte=first)
        .select_related("resource_role")
        .order_by("start_date", "id")
    )
    month_floors = {
        f.resource_role_id: f.min_available
        for f in Floor.objects.filter(team=team, kind=Floor.Kind.MONTH, start_date=first)
    }

    blocks = []
    roles = list(team.resource_roles.filter(active=True).order_by("name"))
    for role in roles:
        counts = {c.date: c for c in services.role_day_counts(team, role, first, last)}
        overridden = set()
        for o in overrides:
            if o.resource_role_id == role.id:
                d = max(o.start_date, first)
                while d <= min(o.end_date, last):
                    overridden.add(d)
                    d += dt.timedelta(days=1)
        weeks = []
        for week in calendar.Calendar(0).monthdatescalendar(first.year, first.month):
            row = []
            for d in week:
                count = counts.get(d)
                row.append(
                    {
                        "date": d,
                        "in_month": d.month == first.month,
                        "working": count is not None,
                        "count": count,
                        "state": _cell_state(count) if count else "off",
                        "override": d in overridden,
                        "available": count.headcount - count.on_leave if count else None,
                    }
                )
            weeks.append(row)
        blocks.append(
            {
                "role": role,
                "headcount": services.headcount(team, role),
                "month_floor": month_floors.get(role.id),
                "weeks": weeks,
            }
        )

    return render(
        request,
        "leave/floors.html",
        {
            "team": team,
            "teams": teams,
            "first": first,
            "month_value": first.strftime("%Y-%m"),
            "prev_month": _shift(first, -1).strftime("%Y-%m"),
            "next_month": _shift(first, 1).strftime("%Y-%m"),
            "blocks": blocks,
            "overrides": overrides,
            "roles": roles,
            "override_form": OverrideFloorForm(roles=roles, initial={"start_date": first, "end_date": first}),
            "weekdays": [calendar.day_abbr[i] for i in range(7)],
        },
    )


def _report_breaches(request, team, first, last):
    """Raising a floor never cancels leave; tell the manager which days now fall short."""
    breaches = services.find_breaches(team, first, last, actor=request.user)
    if not breaches:
        return
    shown = "; ".join(f"{_day(b.date)} {b.role_name} (floor {b.floor}, {b.available} available)" for b in breaches[:6])
    more = f" and {len(breaches) - 6} more" if len(breaches) > 6 else ""
    messages.warning(
        request,
        f"{len(breaches)} day{'s are' if len(breaches) != 1 else ' is'} now below a floor: {shown}{more}. "
        "Existing leave stays in place; cancel or change it from Approvals if needed.",
    )


@login_required
@require_POST
def set_month_floors_view(request):
    _require_manager_role(request.user)
    _, team = _team(request, request.POST.get("team"))
    if team is None:
        return redirect("floors")
    first = _month(request.POST.get("month", ""), dt.date.today())
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    saved, errors = [], []
    for role in team.resource_roles.filter(active=True):
        raw = request.POST.get(f"floor_{role.pk}", "").strip()
        if raw == "":
            continue
        try:
            value = int(raw)
            if not 0 <= value <= MAX_FLOOR:
                raise ValueError
        except ValueError:
            errors.append(f"{role.name}: enter a whole number of 0 or more.")
            continue
        try:
            services.set_month_floor(
                actor=request.user, team=team, resource_role=role, year=first.year, month=first.month, min_available=value
            )
        except LeaveError as exc:
            errors.append(f"{role.name}: {exc}")
            continue
        head = services.headcount(team, role)
        saved.append(f"{role.name} {value}" + (f" (only {head} in the team)" if value > head else ""))
    for error in errors:
        messages.error(request, error)
    if saved:
        messages.success(request, f"Floors for {first:%B %Y} saved: {', '.join(saved)}.")
        _report_breaches(request, team, first, last)
    elif not errors:
        messages.info(request, "Nothing to save. Enter a number for at least one role.")
    return _redirect(team, first)


@login_required
@require_POST
def add_override_view(request):
    _require_manager_role(request.user)
    _, team = _team(request, request.POST.get("team"))
    if team is None:
        return redirect("floors")
    roles = list(team.resource_roles.filter(active=True))
    first = _month(request.POST.get("month", ""), dt.date.today())
    form = OverrideFloorForm(request.POST, roles=roles)
    if not form.is_valid():
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
        return _redirect(team, first)
    data = form.cleaned_data
    try:
        services.set_floor_override(
            actor=request.user,
            team=team,
            resource_role=data["role"],
            start_date=data["start_date"],
            end_date=data["end_date"],
            min_available=data["min_available"],
        )
    except LeaveError as exc:
        messages.error(request, str(exc))
        return _redirect(team, first)
    span = _day(data["start_date"]) + (f" to {_day(data['end_date'])}" if data["end_date"] != data["start_date"] else "")
    messages.success(request, f"{data['role'].name} floor set to {data['min_available']} for {span}.")
    _report_breaches(request, team, data["start_date"], data["end_date"])
    return _redirect(team, data["start_date"].replace(day=1))


@login_required
@require_POST
def delete_override_view(request, pk: int):
    _require_manager_role(request.user)
    floor = get_object_or_404(
        Floor.objects.select_related("team", "resource_role"),
        pk=pk,
        kind=Floor.Kind.OVERRIDE,
        team__in=services.managed_teams(request.user),
    )
    team, first = floor.team, floor.start_date.replace(day=1)
    services.delete_floor_override(actor=request.user, floor=floor)
    messages.success(request, f"Removed the {floor.resource_role.name} override. Those days use the month floor again.")
    return _redirect(team, first)
