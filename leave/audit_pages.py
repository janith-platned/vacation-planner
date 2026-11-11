"""Audit log screen and CSV export.

Managers see the history of their own teams. Administrators see everything, including
organisation-wide changes (regions, departments, manager accounts) that belong to no team.
While an administrator is viewing as a manager, the view is that manager's teams only.
The log is read-only: nothing here can change or delete an entry.
"""
from __future__ import annotations

import csv
import datetime as dt

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render

from accounts.models import SystemRole, Team, User

from . import services
from .manage_pages import _day, _person, _require_manager_role
from .models import AuditLog, LeaveRequest

PAGE_SIZE = 50
CSV_LIMIT = 20000

ACTION_LABELS = {
    "leave.book": "Leave booked",
    "leave.modify": "Leave dates changed",
    "leave.approve": "Request approved",
    "leave.reject": "Request rejected",
    "leave.revoke": "Approved leave rejected",
    "leave.cancel": "Leave cancelled",
    "leave.truncate": "Leave cut back (deactivation)",
    "floor.set_month": "Month floor set",
    "floor.set_override": "Floor override added",
    "floor.delete_override": "Floor override removed",
    "member.add": "Member added",
    "member.update": "Member updated",
    "member.deactivate": "Member deactivated",
    "member.reactivate": "Member reactivated",
    "member.reset_password": "Password reset",
    "team.settings": "Team rules changed",
    "team.add": "Team created",
    "team.update": "Team updated",
    "holiday.add": "Holiday added",
    "holiday.delete": "Holiday removed",
    "leave_type.add": "Leave type added",
    "leave_type.update": "Leave type updated",
    "role.add": "Resource role added",
    "role.update": "Resource role updated",
    "manager.add": "Manager added",
    "region.add": "Region added",
    "region.rename": "Region renamed",
    "region.delete": "Region deleted",
    "department.add": "Department added",
    "department.rename": "Department renamed",
    "department.delete": "Department deleted",
}
CATEGORIES = [
    ("", "All activity"),
    ("leave", "Leave"),
    ("floor", "Floors"),
    ("member", "People"),
    ("settings", "Settings"),
    ("admin", "Administration"),
]
_CATEGORY_PREFIXES = {
    "leave": ["leave."],
    "floor": ["floor."],
    "member": ["member."],
    "settings": ["team.settings", "holiday.", "leave_type.", "role."],
    "admin": ["team.add", "team.update", "manager.", "region.", "department."],
}


def _scope(request):
    """Entries this user may see."""
    user = request.user
    teams = services.managed_teams(user)
    q = Q(team__in=teams)
    if user.system_role == SystemRole.ADMIN and getattr(user, "view_as_manager", None) is None:
        q |= Q(team__isnull=True)  # organisation-wide changes
    return q


def _parse_date(raw):
    try:
        return dt.date.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _filtered(request):
    qs = AuditLog.objects.filter(_scope(request)).select_related("actor", "on_behalf_of", "team")
    p = request.GET
    team_raw = p.get("team", "")
    if team_raw.isdigit():
        qs = qs.filter(team_id=int(team_raw))
    category = p.get("category", "")
    if category in _CATEGORY_PREFIXES:
        cond = Q()
        for prefix in _CATEGORY_PREFIXES[category]:
            cond |= Q(action__startswith=prefix)
        qs = qs.filter(cond)
    for term in p.get("q", "").split()[:5]:
        qs = qs.filter(
            Q(actor__first_name__icontains=term) | Q(actor__last_name__icontains=term) | Q(actor__username__icontains=term)
            | Q(on_behalf_of__first_name__icontains=term) | Q(on_behalf_of__last_name__icontains=term)
            | Q(on_behalf_of__username__icontains=term)
        )
    start, end = _parse_date(p.get("from")), _parse_date(p.get("to"))
    if start:
        qs = qs.filter(created_at__date__gte=start)
    if end:
        qs = qs.filter(created_at__date__lte=end)
    return qs.order_by("-created_at", "-id")


def _targets(entries) -> dict:
    """Readable names for the things the entries are about, fetched in bulk."""
    by_type: dict[str, set] = {}
    for e in entries:
        by_type.setdefault(e.entity_type, set()).add(e.entity_id)
    out = {}
    for r in LeaveRequest.objects.filter(pk__in=by_type.get("LeaveRequest", ())).select_related("user"):
        span = _day(r.start_date) if r.start_date == r.end_date else f"{_day(r.start_date)} to {_day(r.end_date)}"
        out[("LeaveRequest", r.pk)] = f"{_person(r.user)}: {span}"
    for u in User.objects.filter(pk__in=by_type.get("User", ())):
        out[("User", u.pk)] = _person(u)
    for t in Team.objects.filter(pk__in=by_type.get("Team", ())):
        out[("Team", t.pk)] = t.name
    return out


def _detail(e: AuditLog) -> str:
    """A short human summary of what changed, from the stored before/after."""
    after, before = e.after or {}, e.before or {}
    a = e.action
    if a in ("leave.book", "leave.modify") and after.get("start_date"):
        s = f"{after['start_date']} to {after['end_date']}, {after.get('working_days')} working days"
        if after.get("floor_overridden"):
            s += ", floor overridden"
        return s
    if a in ("leave.approve", "leave.reject", "leave.revoke", "leave.cancel"):
        return f"{before.get('status', '')} → {after.get('status', '')}".strip(" →")
    if a == "floor.set_month":
        return f"{after.get('month', '')}: {before.get('min_available', 'none')} → {after.get('min_available')}"
    if a == "floor.set_override":
        return f"{after.get('start_date')} to {after.get('end_date')}: floor {after.get('min_available')}"
    if a == "floor.delete_override":
        return f"{before.get('start_date')} to {before.get('end_date')} (was {before.get('min_available')})"
    if a == "leave.truncate":
        return f"ends {before.get('end_date')} → {after.get('end_date')}"
    if a == "member.deactivate":
        return f"{after.get('cancelled', 0)} cancelled, {after.get('trimmed', 0)} cut back"
    if a in ("holiday.add",):
        return f"{after.get('date')} {after.get('name', '')}"
    if a == "holiday.delete":
        return f"{before.get('date')} {before.get('name', '')}"
    if a == "team.settings":
        changed = [k for k in after if before.get(k) != after.get(k)]
        return "changed: " + ", ".join(changed) if changed else "no change"
    name = after.get("name") or before.get("name")
    return str(name) if name else ""


@login_required
def audit_page(request):
    _require_manager_role(request.user)
    qs = _filtered(request)
    page_obj = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    targets = _targets(page_obj.object_list)
    rows = [
        {
            "entry": e,
            "label": ACTION_LABELS.get(e.action, e.action),
            "target": targets.get((e.entity_type, e.entity_id), ""),
            "detail": _detail(e),
        }
        for e in page_obj.object_list
    ]
    params = {k: v for k, v in request.GET.items() if k != "page" and v}
    from urllib.parse import urlencode

    return render(
        request,
        "leave/audit.html",
        {
            "rows": rows,
            "page_obj": page_obj,
            "teams": list(services.managed_teams(request.user)),
            "categories": CATEGORIES,
            "f": request.GET,
            "base_query": urlencode(params),
            "filtered": bool(params),
        },
    )


def _safe(value) -> str:
    """Stop spreadsheet apps from running a cell that starts like a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@login_required
def audit_csv(request):
    _require_manager_role(request.user)
    entries = list(_filtered(request)[:CSV_LIMIT])
    targets = _targets(entries)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="audit-log.csv"'
    writer = csv.writer(response)
    writer.writerow(["When (UTC)", "Team", "Actor", "On behalf of", "Action", "About", "Summary", "Before", "After"])
    for e in entries:
        writer.writerow(
            [
                _safe(e.created_at.strftime("%Y-%m-%d %H:%M:%S")),
                _safe(e.team.name if e.team else ""),
                _safe(_person(e.actor) if e.actor else "System"),
                _safe(_person(e.on_behalf_of) if e.on_behalf_of else ""),
                _safe(ACTION_LABELS.get(e.action, e.action)),
                _safe(targets.get((e.entity_type, e.entity_id), f"{e.entity_type} {e.entity_id}")),
                _safe(_detail(e)),
                _safe(e.before if e.before is not None else ""),
                _safe(e.after if e.after is not None else ""),
            ]
        )
    return response
