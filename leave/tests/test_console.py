"""Administrator console: filters, view-as, and setup of regions, departments, teams and managers."""
import pytest
from django.urls import reverse

from accounts.models import Department, Region, SystemRole, Team, User
from leave import console_services as svc
from leave import services
from leave.exceptions import InvalidRequest, NotAllowed
from leave.models import AuditLog, LeaveRequest

from .conftest import PASSWORD, TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")


def post(client, name, data=None, *args):
    return client.post(reverse(name, args=args), data or {}, follow=True).content.decode()


@pytest.fixture
def world(admin_user, manager, other_manager, team, make_member):
    """Toronto (manager) in Ontario with an Ops person; Montreal (other_manager) in Quebec with a Dev person."""
    quebec = Region.objects.create(name="Quebec")
    montreal = Team.objects.create(name="Montreal", manager=other_manager, region=quebec)
    ops, dev = Department.objects.create(name="Ops"), Department.objects.create(name="Dev")
    tom = make_member("tom")
    tom.department = ops
    tom.save()
    mia = make_member("mia", team_obj=montreal, role_obj=montreal.resource_roles.get(name="Technical"))
    mia.department = dev
    mia.save()
    return {"toronto": team, "montreal": montreal, "ops": ops, "dev": dev, "quebec": quebec, "tom": tom, "mia": mia}


# ---- access --------------------------------------------------------------------------------- #
def test_only_administrators_can_use_the_console(client, manager, techs, world):
    for person in (manager, techs[0]):
        client.force_login(person)
        assert client.get(reverse("console")).status_code == 403
        for name in ("console-add-team", "console-add-manager", "console-add-region", "console-add-department"):
            assert client.post(reverse(name), {"name": "x"}).status_code == 403
        assert client.post(reverse("view-as-start", args=[manager.pk])).status_code == 403
        assert client.post(reverse("console-update-team", args=[world["toronto"].pk])).status_code == 403


def test_console_link_is_only_for_admins(client, admin_user, manager):
    client.force_login(manager)
    assert reverse("console") not in client.get(reverse("approvals")).content.decode()
    client.force_login(admin_user)
    assert reverse("console") in client.get(reverse("approvals")).content.decode()


# ---- filters ----------------------------------------------------------------------------------- #
def names(body):
    section = body.split("<h2 class=\"sec\">Teams</h2>")[1].split("Add a team")[0]
    return [n for n in ("Toronto", "Montreal") if f"<strong>{n}</strong>" in section]


def test_teams_filter_by_manager_region_department_and_name(client, admin_user, manager, other_manager, world):
    client.force_login(admin_user)
    get = lambda **p: client.get(reverse("console"), p).content.decode()
    assert names(get()) == ["Toronto", "Montreal"] or set(names(get())) == {"Toronto", "Montreal"}
    assert names(get(manager=manager.pk)) == ["Toronto"]
    assert names(get(manager=other_manager.pk)) == ["Montreal"]
    assert names(get(region=world["quebec"].pk)) == ["Montreal"]
    assert names(get(department=world["ops"].pk)) == ["Toronto"]
    assert names(get(department=world["dev"].pk)) == ["Montreal"]
    assert names(get(q="mont")) == ["Montreal"]
    assert names(get(manager=manager.pk, region=world["quebec"].pk)) == []  # filters combine
    assert get(manager="junk").count("<strong>Toronto</strong>") >= 1  # bad filter values are ignored


def test_one_manager_can_span_regions_and_departments(client, admin_user, manager, world):
    other_region = Region.objects.create(name="Alberta")
    Team.objects.create(name="Calgary", manager=manager, region=other_region)
    client.force_login(admin_user)
    body = client.get(reverse("console"), {"manager": manager.pk}).content.decode()
    assert "<strong>Toronto</strong>" in body and "<strong>Calgary</strong>" in body


# ---- view as ------------------------------------------------------------------------------------ #
def test_view_as_shows_only_that_managers_teams_and_can_be_exited(client, admin_user, manager, other_manager, world, vacation):
    services.book_leave(user=world["tom"], leave_type=vacation, start_date=day(0), end_date=day(0), today=TODAY)
    mont_vac = world["montreal"].leave_types.get(name="Vacation")
    services.book_leave(user=world["mia"], leave_type=mont_vac, start_date=day(0), end_date=day(0), today=TODAY)
    client.force_login(admin_user)
    everything = client.get(reverse("approvals"), {"status": "any", "when": "any"}).content.decode()
    assert "Tom Tester" in everything and "Mia Tester" in everything

    body = post(client, "view-as-start", {}, manager.pk)
    assert "Viewing as" in body and "Mona Manager" in body
    scoped = client.get(reverse("approvals"), {"status": "any", "when": "any"}).content.decode()
    assert "Tom Tester" in scoped and "Mia Tester" not in scoped
    assert [t.name for t in services.managed_teams(admin_user)] != []  # a fresh object has no view-as set

    after = post(client, "view-as-stop")
    assert "Viewing as" not in after
    assert "Mia Tester" in client.get(reverse("approvals"), {"status": "any", "when": "any"}).content.decode()


def test_view_as_applies_to_floors_settings_and_team_screens(client, admin_user, manager, other_manager, world):
    client.force_login(admin_user)
    post(client, "view-as-start", {}, other_manager.pk)
    assert "Montreal" in client.get(reverse("settings")).content.decode()
    assert "Toronto" not in client.get(reverse("settings")).content.decode().split("Team rules")[0]
    assert "Mia Tester" in client.get(reverse("members")).content.decode()
    assert "Tom Tester" not in client.get(reverse("members")).content.decode()


def test_view_as_cannot_be_forged_by_non_admins(client, manager, other_manager, world):
    client.force_login(manager)
    session = client.session
    session["view_as_manager_id"] = other_manager.pk
    session.save()
    body = client.get(reverse("members")).content.decode()
    assert "Mia Tester" not in body and "Viewing as" not in body
    assert "view_as_manager_id" not in client.session  # ignored and cleared


def test_view_as_ends_if_the_manager_is_deactivated(client, admin_user, other_manager, world):
    client.force_login(admin_user)
    post(client, "view-as-start", {}, other_manager.pk)
    other_manager.is_active = False
    other_manager.save()
    body = client.get(reverse("approvals")).content.decode()
    assert "Viewing as" not in body and "view_as_manager_id" not in client.session


def test_view_as_only_targets_active_managers(client, admin_user, techs, admin_user_2=None):
    client.force_login(admin_user)
    assert client.post(reverse("view-as-start", args=[techs[0].pk])).status_code == 404
    assert client.post(reverse("view-as-start", args=[admin_user.pk])).status_code == 404


def test_view_as_needs_post(client, admin_user, manager):
    client.force_login(admin_user)
    assert client.get(reverse("view-as-start", args=[manager.pk])).status_code == 405
    assert client.get(reverse("view-as-stop")).status_code == 405


def test_actions_while_viewing_as_are_audited_as_the_administrator(client, admin_user, manager, world, vacation):
    client.force_login(admin_user)
    post(client, "view-as-start", {}, manager.pk)
    leave = services.book_leave(user=world["tom"], leave_type=vacation, start_date=day(0), end_date=day(0), today=TODAY)
    post(client, "reject-approved", {"reason": "Admin decision"}, leave.pk)
    entry = AuditLog.objects.get(action="leave.revoke", entity_id=leave.pk)
    assert entry.actor == admin_user


# ---- regions and departments -------------------------------------------------------------------- #
def test_region_and_department_lifecycle(client, admin_user, world):
    client.force_login(admin_user)
    assert "Region added" in post(client, "console-add-region", {"name": "Prairies"})
    region = Region.objects.get(name="Prairies")
    assert "Region renamed" in post(client, "console-rename-region", {"name": "The Prairies"}, region.pk)
    assert "already exists" in post(client, "console-add-region", {"name": "Quebec"})
    assert "Region deleted" in post(client, "console-delete-region", {}, region.pk)
    assert "still in use" in post(client, "console-delete-region", {}, world["quebec"].pk)
    assert Region.objects.filter(pk=world["quebec"].pk).exists()

    assert "Department added" in post(client, "console-add-department", {"name": "Finance"})
    finance = Department.objects.get(name="Finance")
    assert "Department deleted" in post(client, "console-delete-department", {}, finance.pk)
    assert "still in use" in post(client, "console-delete-department", {}, world["ops"].pk)
    assert "Give the department a name" in post(client, "console-add-department", {"name": " "})


# ---- teams and managers ---------------------------------------------------------------------------- #
def test_create_team_gets_default_roles_and_is_audited(client, admin_user, manager, world):
    client.force_login(admin_user)
    body = post(client, "console-add-team", {"name": "Halifax", "manager": manager.pk, "region": world["quebec"].pk})
    assert "Team created" in body
    team = Team.objects.get(name="Halifax")
    assert team.manager == manager and team.region == world["quebec"]
    assert sorted(team.resource_roles.values_list("name", flat=True)) == ["Functional", "Systems", "Technical"]
    assert AuditLog.objects.filter(action="team.add", entity_id=team.pk, actor=admin_user).exists()
    assert "already exists" in post(client, "console-add-team", {"name": "Halifax", "manager": manager.pk, "region": world["quebec"].pk})


def test_team_manager_must_be_a_manager_or_admin(client, admin_user, techs, world):
    client.force_login(admin_user)
    body = post(client, "console-add-team", {"name": "Bad", "manager": techs[0].pk, "region": world["quebec"].pk})
    assert "Choose an active manager" in body
    assert not Team.objects.filter(name="Bad").exists()
    assert "Choose a manager and a region" in post(client, "console-add-team", {"name": "Bad"})


def test_reassign_a_team_to_another_manager_and_region(client, admin_user, manager, other_manager, world):
    client.force_login(admin_user)
    body = post(client, "console-update-team", {"name": "Toronto East", "manager": other_manager.pk, "region": world["quebec"].pk}, world["toronto"].pk)
    assert "Team updated" in body
    team = world["toronto"]
    team.refresh_from_db()
    assert (team.name, team.manager, team.region) == ("Toronto East", other_manager, world["quebec"])
    assert world["toronto"] in services.managed_teams(other_manager)
    assert world["toronto"] not in services.managed_teams(manager)


def test_cannot_make_a_deactivated_manager_the_team_manager(client, admin_user, other_manager, world):
    other_manager.is_active = False
    other_manager.save()
    client.force_login(admin_user)
    assert "Choose an active manager" in post(
        client, "console-update-team", {"name": "Toronto", "manager": other_manager.pk, "region": world["quebec"].pk}, world["toronto"].pk
    )


def test_add_manager_shows_the_password_once(client, admin_user, world):
    client.force_login(admin_user)
    body = post(client, "console-add-manager", {"first_name": "Mo", "last_name": "New", "username": "mo"})
    user = User.objects.get(username="mo")
    assert user.system_role == SystemRole.MANAGER and user.team_id is None and user.must_change_password
    password = body.split("temporary password: ")[1].split()[0]
    assert user.check_password(password)
    assert "temporary password" not in client.get(reverse("console")).content.decode()
    assert "already exists" in post(client, "console-add-manager", {"first_name": "Mo", "username": "mo"})


def test_services_refuse_non_admins_themselves(manager, world, techs):
    with pytest.raises(NotAllowed):
        svc.create_region(actor=manager, name="Nope")
    with pytest.raises(NotAllowed):
        svc.create_team(actor=manager, name="Nope", manager=manager, region=world["quebec"])
    with pytest.raises(NotAllowed):
        svc.add_manager(actor=techs[0], username="x", first_name="X", last_name="")
    with pytest.raises(NotAllowed):
        svc.delete_department(actor=manager, obj=world["dev"])
    with pytest.raises(InvalidRequest):
        svc.create_region(actor=User.objects.create_user("adm", password=PASSWORD, system_role=SystemRole.ADMIN), name=" ")


# ---- department on a person -------------------------------------------------------------------------- #
def test_manager_sets_a_persons_department(client, manager, world, roles):
    client.force_login(manager)
    tom = world["tom"]
    post(client, "update-member",
         {"first_name": "Tom", "last_name": "Tester", "resource_role": roles["Technical"].pk, "department": world["dev"].pk}, tom.pk)
    tom.refresh_from_db()
    assert tom.department == world["dev"]
    post(client, "update-member", {"first_name": "Tom", "last_name": "Tester", "resource_role": roles["Technical"].pk, "department": ""}, tom.pk)
    tom.refresh_from_db()
    assert tom.department is None
