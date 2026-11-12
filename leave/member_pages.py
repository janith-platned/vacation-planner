"""Manager screens for the people in their teams."""
from __future__ import annotations

import datetime as dt

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import ResourceRole, SystemRole, User

from . import members, services
from .pagination import paginate
from .exceptions import LeaveError
from .floor_pages import _report_breaches
from .forms import AddMemberForm, MemberForm
from .manage_pages import _person, _require_manager_role
from .models import LeaveRequest

STATUS_OPTIONS = [("active", "Active"), ("inactive", "Inactive"), ("any", "Any")]


def _managed_roles(teams):
    return ResourceRole.objects.filter(team__in=teams, active=True).select_related("team").order_by("team__name", "name")


def _member(request, pk: int) -> User:
    return get_object_or_404(
        User.objects.select_related("team", "team__region", "resource_role"),
        pk=pk,
        system_role=SystemRole.MEMBER,
        team__in=services.managed_teams(request.user),
    )


def _flash_password(request, who: str, password: str, action: str) -> None:
    messages.success(
        request,
        f"{action} {who}. Temporary password: {password}  Share it securely; they must change it at first sign-in. "
        "It is not shown again.",
    )


@login_required
def members_page(request):
    _require_manager_role(request.user)
    teams = list(services.managed_teams(request.user))
    selected = request.GET.get("team", "")
    scope = [t for t in teams if str(t.pk) == selected] if selected else teams
    q = request.GET.get("q", "").strip()[:100]
    status = request.GET.get("status", "active")
    status = status if status in dict(STATUS_OPTIONS) else "active"

    qs = (
        User.objects.filter(system_role=SystemRole.MEMBER, team__in=scope)
        .select_related("team", "resource_role")
        .order_by("team__name", "first_name", "last_name", "username")
    )
    for term in q.split():
        qs = qs.filter(Q(first_name__icontains=term) | Q(last_name__icontains=term) | Q(username__icontains=term))
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)

    page_obj, base_query = paginate(request, qs, size=25)
    roles = _managed_roles(teams)
    return render(
        request,
        "leave/members.html",
        {
            "teams": teams,
            "selected_team": selected,
            "q": q,
            "status": status,
            "status_options": STATUS_OPTIONS,
            "members": page_obj.object_list,
            "page_obj": page_obj,
            "base_query": base_query,
            "form": AddMemberForm(roles=roles),
            "multi_team": len(teams) > 1,
        },
    )


@login_required
@require_POST
def add_member_view(request):
    _require_manager_role(request.user)
    teams = list(services.managed_teams(request.user))
    form = AddMemberForm(request.POST, roles=_managed_roles(teams))
    if not form.is_valid():
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
        return redirect("members")
    data = form.cleaned_data
    role = data["resource_role"]
    try:
        user, password = members.add_member(
            actor=request.user,
            team=role.team,
            resource_role=role,
            username=data["username"],
            first_name=data["first_name"],
            last_name=data["last_name"],
            email=data["email"],
            department=data["department"],
        )
    except LeaveError as exc:
        messages.error(request, str(exc))
        return redirect("members")
    _flash_password(request, f"{_person(user)} ({user.username}) added to {role.team.name} as {role.name}.", password, "Added")
    return redirect("member-detail", pk=user.pk)


@login_required
def member_detail_page(request, pk: int):
    _require_manager_role(request.user)
    person = _member(request, pk)
    today = services.today_for(person.team)
    upcoming = LeaveRequest.objects.filter(
        user=person, status__in=LeaveRequest.ACTIVE_STATUSES, end_date__gte=today
    ).select_related("leave_type").order_by("start_date")
    up_page, up_query = paginate(request, upcoming, size=10)
    roles = ResourceRole.objects.filter(team=person.team, active=True).order_by("name")
    return render(
        request,
        "leave/member_detail.html",
        {
            "p": person,
            "form": MemberForm(
                roles=roles,
                initial={
                    "first_name": person.first_name,
                    "last_name": person.last_name,
                    "email": person.email,
                    "resource_role": person.resource_role_id,
                    "department": person.department_id,
                },
            ),
            "upcoming": up_page.object_list,
            "up_page": up_page,
            "up_query": up_query,
        },
    )


@login_required
@require_POST
def update_member_view(request, pk: int):
    _require_manager_role(request.user)
    person = _member(request, pk)
    roles = ResourceRole.objects.filter(team=person.team, active=True)
    form = MemberForm(request.POST, roles=roles)
    if not form.is_valid():
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
        return redirect("member-detail", pk=pk)
    data = form.cleaned_data
    try:
        members.update_member(
            actor=request.user,
            user=person,
            first_name=data["first_name"],
            last_name=data["last_name"],
            email=data["email"],
            resource_role=data["resource_role"],
            department=data["department"],
        )
    except LeaveError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Saved.")
    return redirect("member-detail", pk=pk)


@login_required
@require_POST
def deactivate_member_view(request, pk: int):
    _require_manager_role(request.user)
    person = _member(request, pk)
    try:
        result = members.deactivate_member(actor=request.user, user=person)
    except LeaveError as exc:
        messages.error(request, str(exc))
        return redirect("member-detail", pk=pk)
    parts = []
    if result["cancelled"]:
        parts.append(f"{result['cancelled']} upcoming request{'s' if result['cancelled'] != 1 else ''} cancelled")
    if result["trimmed"]:
        parts.append(f"{result['trimmed']} current request{'s' if result['trimmed'] != 1 else ''} cut back to the days already taken")
    detail = f" ({'; '.join(parts)})" if parts else ""
    messages.success(request, f"{_person(person)} is now inactive and can no longer sign in{detail}.")
    today = services.today_for(person.team)
    _report_breaches(request, person.team, today, today + dt.timedelta(days=89))  # one fewer person in the role
    return redirect("members")


@login_required
@require_POST
def reactivate_member_view(request, pk: int):
    _require_manager_role(request.user)
    person = _member(request, pk)
    try:
        members.reactivate_member(actor=request.user, user=person)
    except LeaveError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{_person(person)} is active again.")
    return redirect("member-detail", pk=pk)


@login_required
@require_POST
def reset_password_view(request, pk: int):
    _require_manager_role(request.user)
    person = _member(request, pk)
    try:
        password = members.reset_password(actor=request.user, user=person)
    except LeaveError as exc:
        messages.error(request, str(exc))
    else:
        _flash_password(request, _person(person), password, "Password reset for")
    return redirect("member-detail", pk=pk)
