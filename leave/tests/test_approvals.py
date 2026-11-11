"""Manager approvals: who can see and decide, and what the manager is told."""
import pytest
from django.urls import reverse

from accounts.models import Region, Team, User
from leave import services
from leave.models import LeaveRequest

from .conftest import PASSWORD, TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")
Status = LeaveRequest.Status


def book(user, leave_type, start, end=None):
    return services.book_leave(user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY)


def set_floor(team, role, manager, value):
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=value)


@pytest.fixture
def pending(team, techs, vacation):
    """Tech1 has a pending request: everything now needs approval."""
    team.approval_threshold = 0
    team.save()
    return book(techs[0], vacation, day(0), day(1))


@pytest.fixture
def other_team(other_manager):
    team = Team.objects.create(name="Vancouver", manager=other_manager, region=Region.objects.create(name="BC"))
    role = team.resource_roles.get(name="Technical")
    member = User.objects.create_user(
        "vic", password=PASSWORD, first_name="Vic", last_name="Other", team=team, resource_role=role,
        must_change_password=False,
    )
    team.approval_threshold = 0
    team.save()
    request = services.book_leave(
        user=member, leave_type=team.leave_types.get(name="Vacation"), start_date=day(0), end_date=day(0), today=TODAY
    )
    return team, member, request


# ---- access ------------------------------------------------------------------ #
def test_members_cannot_open_or_use_the_approvals_screen(client, techs, pending):
    client.force_login(techs[1])
    assert client.get(reverse("approvals")).status_code == 403
    assert client.post(reverse("decide-request", args=[pending.pk]), {"decision": "approve"}).status_code == 403
    pending.refresh_from_db()
    assert pending.status == Status.PENDING


def test_anonymous_visitors_are_sent_to_sign_in(client, pending):
    assert client.get(reverse("approvals")).status_code == 302


def test_manager_sees_names_and_details_for_their_own_teams_only(client, manager, pending, other_team):
    client.force_login(manager)
    body = client.get(reverse("approvals")).content.decode()
    assert "Tech1 Tester" in body and "Technical" in body and "Toronto" in body
    assert "2 working days" in body and "Vacation" in body
    assert "Vic Other" not in body  # another manager's team


def test_admin_sees_every_team(client, admin_user, pending, other_team):
    client.force_login(admin_user)
    body = client.get(reverse("approvals")).content.decode()
    assert "Tech1 Tester" in body and "Vic Other" in body


def test_team_filter_for_a_manager_with_several_teams(client, manager, team, pending):
    second = Team.objects.create(name="Montreal", manager=manager, region=Region.objects.create(name="Quebec"))
    client.force_login(manager)
    assert "Tech1 Tester" in client.get(reverse("approvals"), {"team": team.pk}).content.decode()
    assert "Tech1 Tester" not in client.get(reverse("approvals"), {"team": second.pk}).content.decode()


def test_a_manager_cannot_decide_another_managers_request(client, manager, other_team):
    _, _, request = other_team
    client.force_login(manager)
    assert client.post(reverse("decide-request", args=[request.pk]), {"decision": "approve"}).status_code == 404
    request.refresh_from_db()
    assert request.status == Status.PENDING


def test_deciding_needs_post(client, manager, pending):
    client.force_login(manager)
    assert client.get(reverse("decide-request", args=[pending.pk])).status_code == 405


# ---- deciding ------------------------------------------------------------------- #
def test_approving_keeps_the_days_and_tells_the_employee(client, manager, techs, pending):
    client.force_login(manager)
    response = client.post(
        reverse("decide-request", args=[pending.pk]), {"decision": "approve", "comment": "Enjoy the break"}, follow=True
    )
    body = response.content.decode()
    assert "Approved Tech1 Tester" in body and "Nothing is waiting" in body
    pending.refresh_from_db()
    assert pending.status == Status.APPROVED and pending.decided_by == manager
    assert pending.days.count() == 2

    client.force_login(techs[0])
    mine = client.get(reverse("my-leave")).content.decode()
    assert "Approved" in mine and "Enjoy the break" in mine


def test_rejecting_releases_the_days_and_records_the_reason(client, manager, techs, vacation, pending):
    client.force_login(manager)
    client.post(reverse("decide-request", args=[pending.pk]), {"decision": "reject", "comment": "Release freeze"})
    pending.refresh_from_db()
    assert pending.status == Status.REJECTED and pending.decision_comment == "Release freeze"
    assert pending.days.count() == 0
    client.force_login(techs[0])
    assert "Release freeze" in client.get(reverse("my-leave")).content.decode()


def test_the_comment_is_optional(client, manager, pending):
    client.force_login(manager)
    client.post(reverse("decide-request", args=[pending.pk]), {"decision": "approve"})
    pending.refresh_from_db()
    assert pending.status == Status.APPROVED and pending.decision_comment == ""


def test_an_already_decided_request_is_not_changed_again(client, manager, pending):
    client.force_login(manager)
    client.post(reverse("decide-request", args=[pending.pk]), {"decision": "approve"})
    response = client.post(reverse("decide-request", args=[pending.pk]), {"decision": "reject"}, follow=True)
    assert "Only pending requests" in response.content.decode()
    pending.refresh_from_db()
    assert pending.status == Status.APPROVED


def test_an_unknown_decision_changes_nothing(client, manager, pending):
    client.force_login(manager)
    response = client.post(reverse("decide-request", args=[pending.pk]), {"decision": "maybe"}, follow=True)
    assert "Choose Approve or Reject" in response.content.decode()
    pending.refresh_from_db()
    assert pending.status == Status.PENDING


def test_recently_decided_requests_are_listed_with_who_decided(client, manager, pending):
    client.force_login(manager)
    client.post(reverse("decide-request", args=[pending.pk]), {"decision": "approve", "comment": "OK"})
    body = client.get(reverse("approvals")).content.decode()
    assert "Recently decided" in body and "Mona Manager" in body and "Approved" in body


# ---- the cover information the manager decides on --------------------------------- #
def test_cover_ok_when_the_floor_holds(client, manager, team, roles, pending):
    set_floor(team, roles["Technical"], manager, 2)
    client.force_login(manager)
    assert "Cover holds on every day" in client.get(reverse("approvals")).content.decode()


def test_floor_raised_after_the_request_is_flagged(client, manager, team, roles, techs, vacation, pending):
    set_floor(team, roles["Technical"], manager, 1)
    book(techs[1], vacation, day(0))  # a second person off on Monday; floor 1 of 3 still holds
    set_floor(team, roles["Technical"], manager, 3)  # raised later: 2 off leaves 1 available
    assert [(c.date, c.floor, c.available) for c in services.cover_issues(pending)] == [
        (day(0), 3, 1),
        (day(1), 3, 2),
    ]
    client.force_login(manager)
    body = client.get(reverse("approvals")).content.decode()
    assert "Below the floor on 2 days" in body and "floor 3, 1 available" in body
    assert "Approve" in body  # still the manager's call


def test_leave_types_that_ignore_floors_have_no_cover_issues(team, roles, manager, techs, pending):
    from leave.models import LeaveType

    set_floor(team, roles["Technical"], manager, 3)
    training = LeaveType.objects.create(team=team, name="Training", counts_toward_floor=False, always_needs_approval=True)
    request = book(techs[2], training, day(2))
    assert services.cover_issues(request) == []


# ---- navigation badge --------------------------------------------------------------- #
def test_managers_see_how_many_requests_are_waiting(client, manager, techs, pending):
    client.force_login(manager)
    assert "Approvals <strong>(1)</strong>" in client.get(reverse("calendar")).content.decode()
    client.post(reverse("decide-request", args=[pending.pk]), {"decision": "approve"})
    body = client.get(reverse("calendar")).content.decode()
    assert "Approvals" in body and "<strong>(" not in body


def test_members_do_not_see_the_approvals_link(client, techs, pending):
    client.force_login(techs[1])
    assert "Approvals" not in client.get(reverse("calendar")).content.decode()
