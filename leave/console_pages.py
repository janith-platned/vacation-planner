"""Administrator console: every team, with filters, view-as, and setup of regions, departments, teams, managers."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.middleware import VIEW_AS_SESSION_KEY
from accounts.models import Department, Region, SystemRole, Team, User

from . import console_services as svc
from .pagination import paginate
from .exceptions import LeaveError
from .manage_pages import _person


def _admin_only(request) -> None:
    if request.user.system_role != SystemRole.ADMIN:
        raise PermissionDenied


def _run(request, action, success: str):
    try:
        result = action()
    except LeaveError as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, success)
    return result


def _id(raw):
    return int(raw) if str(raw or "").isdigit() else None


@login_required
def console_page(request):
    _admin_only(request)
    q = request.GET.get("q", "").strip()[:100]
    manager_id, region_id, department_id = (_id(request.GET.get(k)) for k in ("manager", "region", "department"))

    teams = Team.objects.select_related("manager", "region").annotate(
        member_count=Count("members", filter=Q(members__is_active=True, members__system_role=SystemRole.MEMBER), distinct=True)
    )
    if q:
        teams = teams.filter(name__icontains=q)
    if manager_id:
        teams = teams.filter(manager_id=manager_id)
    if region_id:
        teams = teams.filter(region_id=region_id)
    if department_id:
        teams = teams.filter(members__department_id=department_id, members__is_active=True).distinct()
    team_page, team_query = paginate(request, teams.order_by("name"), size=15)
    teams = list(team_page.object_list)
    for team in teams:
        team.departments = sorted(
            set(
                Department.objects.filter(users__team=team, users__is_active=True).values_list("name", flat=True)
            )
        )

    manager_page, manager_query = paginate(
        request,
        User.objects.filter(system_role__in=[SystemRole.MANAGER], is_active=True)
        .select_related("reports_to").annotate(team_count=Count("managed_teams"))
        .order_by("first_name", "last_name", "username"),
        param="mpage",
        size=15,
    )
    managers = list(manager_page.object_list)
    region_page, region_query = paginate(request, Region.objects.annotate(n=Count("teams")).order_by("name"), param="rpage", size=10)
    department_page, department_query = paginate(request, Department.objects.annotate(n=Count("users")).order_by("name"), param="dpage", size=10)
    return render(
        request,
        "leave/console.html",
        {
            "teams": teams,
            "managers": managers,
            "regions": region_page.object_list,
            "departments": department_page.object_list,
            "all_regions": Region.objects.all(),
            "team_page": team_page, "team_query": team_query,
            "manager_page": manager_page, "manager_query": manager_query,
            "region_page": region_page, "region_query": region_query,
            "department_page": department_page, "department_query": department_query,
            "q": q,
            "f_manager": manager_id,
            "f_region": region_id,
            "f_department": department_id,
            "filtered": bool(q or manager_id or region_id or department_id),
            "admins_and_managers": User.objects.filter(
                is_active=True, system_role__in=[SystemRole.MANAGER, SystemRole.ADMIN]
            ).order_by("first_name", "last_name", "username"),
        },
    )


# ---- view as -------------------------------------------------------------------------------- #
@login_required
@require_POST
def view_as_start(request, pk: int):
    _admin_only(request)
    manager = get_object_or_404(User, pk=pk, system_role=SystemRole.MANAGER, is_active=True)
    request.session[VIEW_AS_SESSION_KEY] = manager.pk
    messages.success(request, f"Now viewing as {_person(manager)}.")
    return redirect("approvals")


@login_required
@require_POST
def view_as_stop(request):
    request.session.pop(VIEW_AS_SESSION_KEY, None)
    return redirect("console" if request.user.system_role == SystemRole.ADMIN else "calendar")


# ---- regions and departments ---------------------------------------------------------------- #
def _lookup_views(model, label, create, rename, delete):
    @login_required
    @require_POST
    def add(request):
        _admin_only(request)
        _run(request, lambda: create(actor=request.user, name=request.POST.get("name", "")), f"{label.capitalize()} added.")
        return redirect("console")

    @login_required
    @require_POST
    def edit(request, pk: int):
        _admin_only(request)
        obj = get_object_or_404(model, pk=pk)
        _run(request, lambda: rename(actor=request.user, obj=obj, name=request.POST.get("name", "")), f"{label.capitalize()} renamed.")
        return redirect("console")

    @login_required
    @require_POST
    def remove(request, pk: int):
        _admin_only(request)
        obj = get_object_or_404(model, pk=pk)
        _run(request, lambda: delete(actor=request.user, obj=obj), f"{label.capitalize()} deleted.")
        return redirect("console")

    return add, edit, remove


add_region, rename_region, delete_region = _lookup_views(
    Region, "region", svc.create_region, svc.rename_region, svc.delete_region
)
add_department, rename_department, delete_department = _lookup_views(
    Department, "department", svc.create_department, svc.rename_department, svc.delete_department
)


# ---- teams and managers --------------------------------------------------------------------- #
@login_required
@require_POST
def add_team(request):
    _admin_only(request)
    p = request.POST
    manager = User.objects.filter(pk=_id(p.get("manager"))).first()
    region = Region.objects.filter(pk=_id(p.get("region"))).first()
    if manager is None or region is None:
        messages.error(request, "Choose a manager and a region.")
        return redirect("console")
    _run(request, lambda: svc.create_team(actor=request.user, name=p.get("name", ""), manager=manager, region=region),
         "Team created with the default roles. Its manager can now set up floors and leave types.")
    return redirect("console")


@login_required
@require_POST
def update_team(request, pk: int):
    _admin_only(request)
    team = get_object_or_404(Team, pk=pk)
    p = request.POST
    manager = User.objects.filter(pk=_id(p.get("manager"))).first()
    region = Region.objects.filter(pk=_id(p.get("region"))).first()
    if manager is None or region is None:
        messages.error(request, "Choose a manager and a region.")
        return redirect("console")
    _run(request, lambda: svc.update_team(actor=request.user, team=team, name=p.get("name", ""), manager=manager, region=region),
         "Team updated.")
    return redirect("console")


@login_required
@require_POST
def set_reports_to(request, pk: int):
    _admin_only(request)
    manager = get_object_or_404(User, pk=pk, system_role=SystemRole.MANAGER)
    boss = User.objects.filter(pk=_id(request.POST.get("reports_to"))).first()
    _run(request, lambda: svc.set_reports_to(actor=request.user, manager=manager, boss=boss), "Reporting line saved.")
    return redirect("console")


@login_required
@require_POST
def add_manager(request):
    _admin_only(request)
    p = request.POST
    result = _run(
        request,
        lambda: svc.add_manager(
            actor=request.user, username=p.get("username", ""), first_name=p.get("first_name", ""),
            last_name=p.get("last_name", ""), email=p.get("email", ""),
            reports_to=User.objects.filter(pk=_id(p.get("reports_to")), is_active=True).first(),
        ),
        "Manager added.",
    )
    if result:
        user, password = result
        messages.success(
            request,
            f"{_person(user)} ({user.username}) can sign in with the temporary password: {password}  "
            "Share it securely; they must change it at first sign-in. It is not shown again.",
        )
    return redirect("console")
