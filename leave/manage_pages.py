"""Manager and administrator screens.

Unlike the member screens these show people's names, so every view checks the
role on the server and only returns teams the user manages.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import SystemRole

from . import services
from .exceptions import LeaveError
from .models import LeaveRequest

Status = LeaveRequest.Status


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


@login_required
def approvals_page(request):
    user = request.user
    _require_manager_role(user)
    teams = list(services.managed_teams(user))
    selected = request.GET.get("team")
    scope = [t for t in teams if str(t.pk) == selected] if selected else teams

    pending = []
    base = LeaveRequest.objects.select_related("user", "team", "team__region", "leave_type")
    for leave_request in base.filter(team__in=scope, status=Status.PENDING).order_by("start_date", "id"):
        first_day = leave_request.days.select_related("resource_role").first()
        pending.append(
            {
                "request": leave_request,
                "role": first_day.resource_role.name if first_day else "",
                "issues": services.cover_issues(leave_request),
            }
        )
    decided = base.filter(team__in=scope, decided_at__isnull=False).select_related("decided_by")
    return render(
        request,
        "leave/approvals.html",
        {
            "teams": teams,
            "selected_team": selected,
            "pending": pending,
            "recent": decided.order_by("-decided_at")[:15],
        },
    )


@login_required
@require_POST
def decide_request_view(request, pk: int):
    user = request.user
    _require_manager_role(user)
    leave_request = get_object_or_404(
        LeaveRequest.objects.select_related("user", "team"), pk=pk, team__in=services.managed_teams(user)
    )
    decision = request.POST.get("decision")
    if decision not in ("approve", "reject"):
        messages.error(request, "Choose Approve or Reject.")
        return redirect("approvals")
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
    return redirect("approvals")
