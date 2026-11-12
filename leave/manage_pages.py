"""Manager and administrator screens.

Unlike the member screens these show people's names, so every view checks the
role on the server and only returns teams the user manages.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from accounts.models import ResourceRole, SystemRole, User

from . import services
from .pagination import paginate
from .exceptions import FloorViolationError, LeaveError
from .forms import LogLeaveForm, ManagerModifyForm
from .models import AuditLog, LeaveRequest, LeaveType

Status = LeaveRequest.Status
PAGE_SIZE = 20

STATUS_FILTERS = {
    "approved": (Status.APPROVED,),
    "pending": (Status.PENDING,),
    "rejected": (Status.REJECTED,),
    "cancelled": (Status.CANCELLED_BY_EMPLOYEE, Status.CANCELLED_BY_MANAGER),
    "any": None,
}
STATUS_LABELS = [
    ("approved", "Approved"),
    ("pending", "Pending"),
    ("rejected", "Rejected"),
    ("cancelled", "Cancelled"),
    ("any", "Any status"),
]
WHEN_LABELS = [("upcoming", "Upcoming and current"), ("past", "Past"), ("any", "Any time")]
ACTION_LABELS = {
    "leave.book": "Booked",
    "leave.modify": "Dates changed",
    "leave.approve": "Approved",
    "leave.reject": "Rejected",
    "leave.revoke": "Approved leave rejected",
    "leave.cancel": "Cancelled",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _require_manager_role(user) -> None:
    if user.system_role == SystemRole.MEMBER:
        raise PermissionDenied


def _person(user) -> str:
    return user.get_full_name() or user.username


def _day(d) -> str:
    return f"{d:%a} {d.day} {d:%b %Y}"  # no %-d: it is not portable to Windows


def _dates(leave_request: LeaveRequest) -> str:
    if leave_request.end_date == leave_request.start_date:
        return _day(leave_request.start_date)
    return f"{_day(leave_request.start_date)} to {_day(leave_request.end_date)}"


def _back(request, default: str = "approvals"):
    """Go back to the page the action came from, if it is one of ours."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect(default)


def _managed_request(request, pk: int) -> LeaveRequest:
    return get_object_or_404(
        LeaveRequest.objects.select_related("user", "team", "team__region", "leave_type"),
        pk=pk,
        team__in=services.managed_teams(request.user),
    )


def _violation_text(violations) -> str:
    return "; ".join(f"{_day(v.date)} (floor {v.floor}, {v.available_after} available)" for v in violations)


# --------------------------------------------------------------------------- #
# Approvals and the requests table
# --------------------------------------------------------------------------- #
@login_required
def approvals_page(request):
    user = request.user
    _require_manager_role(user)
    teams = list(services.managed_teams(user))
    selected = request.GET.get("team", "")
    scope = [t for t in teams if str(t.pk) == selected] if selected else teams

    q = request.GET.get("q", "").strip()[:100]
    role = request.GET.get("role", "")
    status = request.GET.get("status", "approved")
    status = status if status in STATUS_FILTERS else "approved"
    when = request.GET.get("when", "upcoming")
    when = when if when in dict(WHEN_LABELS) else "upcoming"

    base = LeaveRequest.objects.select_related("user", "team", "team__region", "leave_type", "user__resource_role", "decided_by")

    # Requests waiting for a decision: few, and each needs the manager's attention.
    pending = []
    pending_page, pending_query = paginate(
        request, base.filter(team__in=scope, status=Status.PENDING).order_by("start_date", "id"), param="ppage", size=10
    )
    for leave_request in pending_page.object_list:
        first_day = leave_request.days.select_related("resource_role").first()
        pending.append(
            {
                "request": leave_request,
                "role": first_day.resource_role.name if first_day else "",
                "issues": services.cover_issues(leave_request),
            }
        )

    # Everything else: a searchable, filterable, paginated table.
    qs = base.filter(team__in=scope)
    for term in q.split():
        qs = qs.filter(
            Q(user__first_name__icontains=term) | Q(user__last_name__icontains=term) | Q(user__username__icontains=term)
        )
    if role:
        qs = qs.filter(user__resource_role__name=role)
    statuses = STATUS_FILTERS[status]
    if statuses:
        qs = qs.filter(status__in=statuses)
    if when != "any":
        cond = Q()
        for team in scope:
            today = services.today_for(team)
            cond |= Q(team=team, end_date__gte=today) if when == "upcoming" else Q(team=team, end_date__lt=today)
        qs = qs.filter(cond) if scope else qs.none()
    qs = qs.order_by("start_date", "id") if when == "upcoming" else qs.order_by("-start_date", "-id")
    page_obj = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))

    filters = {"team": selected, "q": q, "role": role, "status": status, "when": when}
    return render(
        request,
        "leave/approvals.html",
        {
            "teams": teams,
            "selected_team": selected,
            "q": q,
            "role": role,
            "roles": sorted(
                set(ResourceRole.objects.filter(team__in=scope, active=True).values_list("name", flat=True))
            ),
            "status": status,
            "status_labels": STATUS_LABELS,
            "when": when,
            "when_labels": WHEN_LABELS,
            "pending": pending,
            "pending_total": pending_page.paginator.count,
            "pending_page": pending_page,
            "pending_query": pending_query,
            "page_obj": page_obj,
            "base_query": urlencode({k: v for k, v in filters.items() if v}),
            "filtered": bool(q or role or selected or status != "approved" or when != "upcoming"),
        },
    )


@login_required
def request_detail_page(request, pk: int):
    _require_manager_role(request.user)
    leave_request = _managed_request(request, pk)
    today = services.today_for(leave_request.team)
    active = leave_request.status in LeaveRequest.ACTIVE_STATUSES

    history = []
    logs = AuditLog.objects.filter(entity_type="LeaveRequest", entity_id=leave_request.pk)
    for log in logs.select_related("actor", "on_behalf_of").order_by("created_at", "id"):
        after = log.after or {}
        detail = ""
        if log.action in ("leave.book", "leave.modify") and after.get("start_date"):
            start = dt.date.fromisoformat(after["start_date"])
            end = dt.date.fromisoformat(after["end_date"])
            detail = f"{_day(start)}" + (f" to {_day(end)}" if end != start else "")
            detail += f", {after.get('working_days')} working days"
            if after.get("floor_overridden"):
                detail += ", floor overridden"
        history.append(
            {
                "when": log.created_at,
                "label": ACTION_LABELS.get(log.action, log.action),
                "who": _person(log.actor) if log.actor else "System",
                "on_behalf_of": _person(log.on_behalf_of) if log.on_behalf_of else "",
                "detail": detail,
            }
        )

    user = leave_request.user
    return render(
        request,
        "leave/request_detail.html",
        {
            "r": leave_request,
            "role": user.resource_role.name if user.resource_role_id else "",
            "days": list(leave_request.days.order_by("date").values_list("date", flat=True)),
            "issues": services.cover_issues(leave_request) if active else [],
            "can_decide": leave_request.status == Status.PENDING,
            "can_change": leave_request.status == Status.APPROVED and leave_request.end_date >= today,
            "history": history,
        },
    )


@login_required
@require_POST
def decide_request_view(request, pk: int):
    user = request.user
    _require_manager_role(user)
    leave_request = _managed_request(request, pk)
    decision = request.POST.get("decision")
    if decision not in ("approve", "reject"):
        messages.error(request, "Choose Approve or Reject.")
        return _back(request)
    approve = decision == "approve"
    try:
        services.decide_request(
            request=leave_request,
            actor=user,
            approve=approve,
            comment=request.POST.get("comment", "").strip()[:500],
        )
    except LeaveError as exc:
        messages.error(request, str(exc))
    else:
        verb = "Approved" if approve else "Rejected"
        messages.success(request, f"{verb} {_person(leave_request.user)}'s leave ({_dates(leave_request)}).")
    return _back(request)


@login_required
@require_POST
def modify_request_view(request, pk: int):
    """Change the dates of approved leave. Checked against the floor like a new booking."""
    _require_manager_role(request.user)
    leave_request = _managed_request(request, pk)
    if leave_request.status != Status.APPROVED:
        messages.error(request, "Only approved leave can be modified here.")
        return _back(request)
    form = ManagerModifyForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(error for errors in form.errors.values() for error in errors))
        return _back(request)
    data = form.cleaned_data
    try:
        updated = services.modify_leave(
            request=leave_request,
            actor=request.user,
            start_date=data["start_date"],
            end_date=data["end_date"],
            override_floor=data["override_floor"],
            override_reason=data["override_reason"],
            comment=data["comment"],
        )
    except FloorViolationError as exc:
        messages.error(
            request,
            f"Not changed. {_person(leave_request.user)} would take the role below its floor on: "
            f"{_violation_text(exc.violations)}. Tick 'Override the floor' and give a reason to go ahead.",
        )
    except LeaveError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Updated {_person(updated.user)}'s leave to {_dates(updated)}.")
    return _back(request)


@login_required
@require_POST
def reject_approved_view(request, pk: int):
    """Reject leave that was already approved. A reason is required and shown to the employee."""
    _require_manager_role(request.user)
    leave_request = _managed_request(request, pk)
    try:
        services.revoke_approved_leave(
            request=leave_request, actor=request.user, reason=request.POST.get("reason", "").strip()[:500]
        )
    except LeaveError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Rejected {_person(leave_request.user)}'s approved leave ({_dates(leave_request)}). The days are released.",
        )
    return _back(request)


# --------------------------------------------------------------------------- #
# Logging leave on behalf of an employee
# --------------------------------------------------------------------------- #
@login_required
def log_leave_page(request):
    user = request.user
    _require_manager_role(user)
    teams = list(services.managed_teams(user))
    initial = {"employee": request.GET["employee"]} if request.method == "GET" and request.GET.get("employee") else None
    form = LogLeaveForm(request.POST or None, teams=teams, manager=user, initial=initial)
    violations = []
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        employee = data["employee"]
        try:
            booking = services.book_leave(
                user=employee,
                leave_type=data["leave_type"],
                start_date=data["start_date"],
                end_date=data["end_date"],
                actor=user,
                override_floor=data["override_floor"],
                override_reason=data["override_reason"],
                comment=data["comment"],
            )
        except FloorViolationError as exc:
            violations = exc.violations
        except LeaveError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Logged {_person(employee)}'s leave: {_dates(booking)} (approved).")
            return redirect("request-detail", pk=booking.pk)

    employees = list(form.fields["employee"].queryset)
    return render(
        request,
        "leave/log_leave.html",
        {
            "form": form,
            "violations": violations,
            "violation_text": _violation_text(violations) if violations else "",
            "employee_team": {e.pk: e.team_id for e in employees},
            "type_team": {lt.pk: lt.team_id for lt in form.fields["leave_type"].queryset},
            "has_employees": bool(employees),
        },
    )


@login_required
@require_GET
def manager_check(request):
    """Advisory preview for the log-leave form: what would happen if this were booked now."""
    _require_manager_role(request.user)
    try:
        employee_id = int(request.GET["employee"])
        type_id = int(request.GET["leave_type"])
        start = dt.date.fromisoformat(request.GET["start"])
        end = dt.date.fromisoformat(request.GET["end"])
    except (KeyError, ValueError):
        return JsonResponse({"error": "employee, leave_type, start and end are required"}, status=400)
    employee = (
        User.objects.select_related("team", "resource_role")
        .filter(pk=employee_id, team__in=services.managed_teams(request.user))
        .first()
    )
    if employee is None:
        return JsonResponse({"error": "unknown employee"}, status=404)
    leave_type = LeaveType.objects.filter(pk=type_id, team_id=employee.team_id).first()
    if leave_type is None:
        return JsonResponse({"error": "unknown leave type"}, status=400)
    preview = services.preview_booking(
        user=employee, leave_type=leave_type, start_date=start, end_date=end, privileged=True
    )
    return JsonResponse(
        {
            "working_days": preview.working_days,
            "status": preview.status,
            "problem": preview.problem,
            "violations": [
                {"date": v.date.isoformat(), "floor": v.floor, "available_after": v.available_after}
                for v in preview.violations
            ],
        }
    )
