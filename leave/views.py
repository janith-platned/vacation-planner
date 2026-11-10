"""Read-only calendar API.

A team member only ever receives anonymous aggregates for their own resource role.
Names and booking ids are never serialised here.
"""
from __future__ import annotations

import datetime as dt

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from accounts.models import Team

from . import services
from .exceptions import InvalidRequest

MAX_CALENDAR_DAYS = 93


def _day_payload(counts: list[services.DayCount]) -> list[dict]:
    return [
        {
            "date": c.date.isoformat(),
            "headcount": c.headcount,
            "on_leave": c.on_leave,
            "floor": c.floor,
            "slots_left": c.slots_left,
        }
        for c in counts
    ]


@login_required
@require_GET
def calendar_counts(request):
    try:
        start = dt.date.fromisoformat(request.GET["start"])
        end = dt.date.fromisoformat(request.GET["end"])
    except (KeyError, ValueError):
        return JsonResponse({"error": "start and end (YYYY-MM-DD) are required"}, status=400)
    if end < start or (end - start).days > MAX_CALENDAR_DAYS:
        return JsonResponse({"error": f"range must be 0-{MAX_CALENDAR_DAYS} days"}, status=400)

    user = request.user
    team_param = request.GET.get("team")
    if team_param:
        # Managers and administrators may look at any team they manage, all roles.
        try:
            team = Team.objects.filter(pk=int(team_param)).first()
        except ValueError:
            return JsonResponse({"error": "invalid team"}, status=400)
        if team is None or not services.is_team_manager(user, team):
            return JsonResponse({"error": "forbidden"}, status=403)
        roles = list(team.resource_roles.filter(active=True))
    else:
        # Everyone else sees their own role in their own team, nothing more.
        if not user.team_id or not user.resource_role_id:
            return JsonResponse({"error": "forbidden"}, status=403)
        team = user.team
        roles = [user.resource_role]

    try:
        payload = {
            "roles": [
                {"role": role.name, "days": _day_payload(services.role_day_counts(team, role, start, end))}
                for role in roles
            ]
        }
    except InvalidRequest as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(payload)
