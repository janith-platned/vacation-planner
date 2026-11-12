"""Team member management: services and screens."""
import datetime as dt

import pytest
from django.urls import reverse

from accounts.models import Region, SystemRole, Team, User
from leave import members, services
from leave.exceptions import InvalidRequest, NotAllowed
from leave.models import AuditLog, LeaveDay, LeaveRequest

from .conftest import PASSWORD, TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")
Status = LeaveRequest.Status


def book(user, leave_type, start, end=None, **kwargs):
    return services.book_leave(
        user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY, **kwargs
    )


def add(manager, team, role, username="newbie", **kw):
    return members.add_member(
        actor=manager, team=team, resource_role=role, username=username,
        first_name=kw.get("first_name", "Nina"), last_name=kw.get("last_name", "Newbie"), email=kw.get("email", ""),
    )


# ---- services: adding ------------------------------------------------------------------ #
def test_add_member_creates_a_member_with_a_temporary_password(manager, team, roles):
    user, password = add(manager, team, roles["Technical"])
    assert user.system_role == SystemRole.MEMBER and user.team == team and user.resource_role == roles["Technical"]
    assert user.must_change_password and user.check_password(password) and len(password) >= 12
    assert AuditLog.objects.filter(action="member.add", entity_id=user.pk, actor=manager).exists()
    assert services.headcount(team, roles["Technical"]) == 1


def test_passwords_are_random(manager, team, roles):
    assert members.generate_temp_password() != members.generate_temp_password()


def test_add_member_validates(manager, team, roles, other_manager):
    add(manager, team, roles["Technical"])
    with pytest.raises(InvalidRequest, match="already taken|already exists"):
        add(manager, team, roles["Technical"])
    with pytest.raises(InvalidRequest):
        add(manager, team, roles["Technical"], username=" ", first_name="X")
    with pytest.raises(InvalidRequest):
        add(manager, team, roles["Technical"], username="nofirst", first_name=" ")
    elsewhere = Team.objects.create(name="Elsewhere", manager=other_manager, region=Region.objects.create(name="E"))
    with pytest.raises(InvalidRequest):
        add(manager, team, elsewhere.resource_roles.first(), username="wrongteam")  # role from another team
    with pytest.raises(NotAllowed):
        add(other_manager, team, roles["Technical"], username="intruder")


def test_inactive_role_cannot_be_used(manager, team, roles):
    roles["Systems"].active = False
    roles["Systems"].save()
    with pytest.raises(InvalidRequest):
        add(manager, team, roles["Systems"])


# ---- services: updating ----------------------------------------------------------------- #
def test_update_details_and_role(manager, team, roles, techs):
    user = members.update_member(
        actor=manager, user=techs[0], first_name="Terry", last_name="T", email="t@example.com",
        resource_role=roles["Functional"], today=TODAY,
    )
    assert user.first_name == "Terry" and user.resource_role == roles["Functional"]
    assert services.headcount(team, roles["Technical"]) == 2 and services.headcount(team, roles["Functional"]) == 1


def test_role_change_is_refused_while_leave_is_booked(manager, roles, techs, vacation):
    book(techs[0], vacation, day(0))
    with pytest.raises(InvalidRequest, match="Cancel it first"):
        members.update_member(
            actor=manager, user=techs[0], first_name="Tech1", last_name="Tester", email="",
            resource_role=roles["Functional"], today=TODAY,
        )
    # renaming without changing the role is fine
    members.update_member(
        actor=manager, user=techs[0], first_name="Renamed", last_name="Tester", email="",
        resource_role=roles["Technical"], today=TODAY,
    )


def test_past_leave_does_not_block_a_role_change(manager, roles, techs, vacation):
    book(techs[0], vacation, day(0))
    members.update_member(
        actor=manager, user=techs[0], first_name="Tech1", last_name="Tester", email="",
        resource_role=roles["Functional"], today=day(5),  # that week is over
    )


def test_only_the_teams_manager_can_update(other_manager, roles, techs):
    with pytest.raises(NotAllowed):
        members.update_member(
            actor=other_manager, user=techs[0], first_name="X", last_name="", email="",
            resource_role=roles["Technical"], today=TODAY,
        )


def test_managers_and_admins_are_not_managed_here(manager, admin_user, roles):
    with pytest.raises(InvalidRequest):
        members.deactivate_member(actor=admin_user, user=manager, today=TODAY)


# ---- services: deactivating ------------------------------------------------------------------ #
def test_deactivation_cancels_future_leave_and_removes_them_from_headcount(manager, team, roles, techs, vacation):
    future = book(techs[0], vacation, day(0), day(1))
    result = members.deactivate_member(actor=manager, user=techs[0], today=TODAY)
    assert result == {"cancelled": 1, "trimmed": 0}
    future.refresh_from_db()
    techs[0].refresh_from_db()
    assert not techs[0].is_active
    assert future.status == Status.CANCELLED_BY_MANAGER and future.cancel_reason == "Account deactivated."
    assert not LeaveDay.objects.filter(user=techs[0]).exists()
    assert services.headcount(team, roles["Technical"]) == 2
    assert AuditLog.objects.filter(action="member.deactivate", entity_id=techs[0].pk).exists()


def test_leave_in_progress_is_cut_back_to_the_days_taken(manager, techs, vacation):
    current = book(techs[0], vacation, day(0), day(4))  # Mon-Fri
    members.deactivate_member(actor=manager, user=techs[0], today=day(2))  # Wednesday
    current.refresh_from_db()
    assert current.status == Status.APPROVED and current.end_date == day(1) and current.working_days == 2
    assert list(current.days.order_by("date").values_list("date", flat=True)) == [day(0), day(1)]
    assert AuditLog.objects.filter(action="leave.truncate", entity_id=current.pk).exists()


def test_past_leave_is_untouched(manager, techs, vacation):
    old = book(techs[0], vacation, day(0))
    result = members.deactivate_member(actor=manager, user=techs[0], today=day(10))
    assert result == {"cancelled": 0, "trimmed": 0}
    old.refresh_from_db()
    assert old.status == Status.APPROVED and old.days.count() == 1


def test_deactivated_leave_no_longer_blocks_others(manager, team, roles, techs, vacation):
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=2)
    book(techs[0], vacation, day(0))
    members.deactivate_member(actor=manager, user=techs[0], today=TODAY)
    # 2 active techs left, floor 2: nobody can take a day off, and tech1's old day is not double counted
    assert [v.date for v in services.check_floor(team, roles["Technical"], [day(0)])] == [day(0)]
    assert services.booked_counts(team, roles["Technical"], [day(0)]) == {}


def test_cannot_deactivate_twice(manager, techs):
    members.deactivate_member(actor=manager, user=techs[0], today=TODAY)
    techs[0].refresh_from_db()
    with pytest.raises(InvalidRequest):
        members.deactivate_member(actor=manager, user=techs[0], today=TODAY)


def test_reactivate_and_reset_password(manager, techs, roles):
    members.deactivate_member(actor=manager, user=techs[0], today=TODAY)
    techs[0].refresh_from_db()
    members.reactivate_member(actor=manager, user=techs[0])
    techs[0].refresh_from_db()
    assert techs[0].is_active
    password = members.reset_password(actor=manager, user=techs[0])
    techs[0].refresh_from_db()
    assert techs[0].check_password(password) and techs[0].must_change_password
    assert not techs[0].check_password(PASSWORD)


def test_cannot_reactivate_into_an_inactive_role(manager, techs, roles):
    members.deactivate_member(actor=manager, user=techs[0], today=TODAY)
    roles["Technical"].active = False
    roles["Technical"].save()
    techs[0].refresh_from_db()
    with pytest.raises(InvalidRequest):
        members.reactivate_member(actor=manager, user=techs[0])


# ---- screens ---------------------------------------------------------------------------------- #
def test_members_page_lists_filters_and_hides_other_teams(client, manager, other_manager, team, techs, make_member):
    elsewhere = Team.objects.create(name="Elsewhere", manager=other_manager, region=Region.objects.create(name="E"))
    make_member("outsider", team_obj=elsewhere, role_obj=elsewhere.resource_roles.first())
    members.deactivate_member(actor=manager, user=techs[2], today=TODAY)
    client.force_login(manager)
    body = client.get(reverse("members")).content.decode()
    assert "Tech1 Tester" in body and "Tech2 Tester" in body and "Tech3 Tester" not in body  # inactive hidden
    assert "Outsider" not in body
    assert "Tech3 Tester" in client.get(reverse("members"), {"status": "inactive"}).content.decode()
    only = client.get(reverse("members"), {"q": "tech2", "status": "any"}).content.decode()
    assert "Tech2 Tester" in only and "Tech1 Tester" not in only


def test_members_cannot_use_any_member_screen(client, techs):
    client.force_login(techs[0])
    assert client.get(reverse("members")).status_code == 403
    assert client.post(reverse("add-member"), {}).status_code == 403
    for name in ("member-detail", "update-member", "deactivate-member", "reactivate-member", "reset-member-password"):
        method = client.get if name == "member-detail" else client.post
        assert method(reverse(name, args=[techs[1].pk])).status_code == 403


def test_adding_through_the_screen_shows_the_password_once(client, manager, team, roles):
    client.force_login(manager)
    response = client.post(
        reverse("add-member"),
        {"first_name": "Nina", "last_name": "Newbie", "username": "nina", "email": "", "resource_role": roles["Technical"].pk},
        follow=True,
    )
    body = response.content.decode()
    user = User.objects.get(username="nina")
    assert "Temporary password:" in body and "Nina Newbie" in body and user.must_change_password
    password = body.split("Temporary password: ")[1].split()[0]
    assert user.check_password(password)
    assert "Temporary password:" not in client.get(reverse("members")).content.decode()  # not shown again


def test_screen_explains_duplicate_usernames_and_foreign_roles(client, manager, other_manager, team, roles, techs):
    elsewhere = Team.objects.create(name="Elsewhere", manager=other_manager, region=Region.objects.create(name="E"))
    client.force_login(manager)
    dup = client.post(
        reverse("add-member"),
        {"first_name": "Dup", "username": "tech1", "resource_role": roles["Technical"].pk}, follow=True,
    )
    assert "already" in dup.content.decode()
    foreign = client.post(
        reverse("add-member"),
        {"first_name": "F", "username": "foreigner", "resource_role": elsewhere.resource_roles.first().pk}, follow=True,
    )
    assert "Select a valid choice" in foreign.content.decode()
    assert not User.objects.filter(username="foreigner").exists()


def test_other_managers_members_are_404(client, other_manager, techs):
    client.force_login(other_manager)
    assert client.get(reverse("member-detail", args=[techs[0].pk])).status_code == 404
    for name in ("update-member", "deactivate-member", "reactivate-member", "reset-member-password"):
        assert client.post(reverse(name, args=[techs[0].pk])).status_code == 404
    techs[0].refresh_from_db()
    assert techs[0].is_active


def test_managers_themselves_are_404_on_member_screens(client, manager, other_manager, team):
    client.force_login(manager)
    assert client.get(reverse("member-detail", args=[manager.pk])).status_code == 404


def test_deactivating_through_the_screen_reports_what_happened_and_warns_about_floors(
    client, manager, team, roles, techs, vacation
):
    book(techs[0], vacation, day(0))  # two of three off, before any floor exists
    book(techs[1], vacation, day(0))
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=1)
    client.force_login(manager)
    # losing the one person still available leaves nobody against a floor of 1
    body = client.post(reverse("deactivate-member", args=[techs[2].pk]), follow=True).content.decode()
    assert "Tech3 Tester is now inactive" in body
    assert "now below a floor" in body and "Technical (floor 1, 0 available)" in body
    techs[2].refresh_from_db()
    assert not techs[2].is_active


def test_detail_page_shows_upcoming_leave_and_actions(client, manager, techs, vacation):
    book(techs[0], vacation, day(0))
    client.force_login(manager)
    body = client.get(reverse("member-detail", args=[techs[0].pk])).content.decode()
    assert "Upcoming Leave" in body and "Mon 7 Dec 2026" in body and "Deactivate" in body and "Reset Password" in body


def test_updating_through_the_screen(client, manager, roles, techs, vacation):
    client.force_login(manager)
    url = reverse("update-member", args=[techs[0].pk])
    ok = client.post(url, {"first_name": "Terry", "last_name": "Tester", "email": "t@example.com", "resource_role": roles["Functional"].pk}, follow=True)
    assert "Saved." in ok.content.decode()
    techs[0].refresh_from_db()
    assert techs[0].first_name == "Terry" and techs[0].resource_role == roles["Functional"]
    book(techs[0], vacation, day(0))
    refused = client.post(url, {"first_name": "Terry", "last_name": "Tester", "resource_role": roles["Technical"].pk}, follow=True)
    assert "Cancel it first" in refused.content.decode()


def test_a_deactivated_person_cannot_sign_in(client, manager, techs):
    members.deactivate_member(actor=manager, user=techs[0], today=TODAY)
    assert not client.login(username="tech1", password=PASSWORD)
