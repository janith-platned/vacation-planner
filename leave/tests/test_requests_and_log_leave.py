"""The requests table, the request detail page, and managers logging leave for employees."""
import datetime as dt

import pytest
from django.urls import reverse

from accounts.models import Region, Team
from leave import services
from leave.exceptions import NotAllowed
from leave.models import AuditLog, LeaveRequest, LeaveType

from .conftest import TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")
Status = LeaveRequest.Status


def book(user, leave_type, start, end=None, **kwargs):
    return services.book_leave(
        user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY, **kwargs
    )


def set_floor(team, role, manager, value):
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=value)


def listing(client, **params):
    return client.get(reverse("approvals"), params).content.decode().split("All Requests")[1]


# ---- the table ------------------------------------------------------------------- #
def test_search_filters_by_name(client, manager, techs, vacation):
    book(techs[0], vacation, day(0))
    book(techs[1], vacation, day(1))
    client.force_login(manager)
    body = listing(client, q="tech2")
    assert "Tech2 Tester" in body and "Tech1 Tester" not in body


def test_filter_by_role_and_status(client, manager, roles, make_member, vacation):
    func = make_member("fran", role="Functional")
    tech = make_member("tess", role="Technical")
    book(func, vacation, day(0))
    book(tech, vacation, day(1))
    client.force_login(manager)
    only_func = listing(client, role="Functional")
    assert "Fran Tester" in only_func and "Tess Tester" not in only_func
    assert "No requests match" in listing(client, status="rejected")
    services.cancel_leave(request=LeaveRequest.objects.get(user=tech), actor=tech, today=TODAY)
    assert "Tess Tester" in listing(client, status="cancelled")
    assert "Tess Tester" not in listing(client)  # default view is approved only


def test_pagination_keeps_the_filters(client, manager, techs, vacation):
    for week in range(25):
        book(techs[0], vacation, day(0) + dt.timedelta(days=7 * week))
    book(techs[1], vacation, day(1))
    client.force_login(manager)
    first = listing(client, q="tech1")
    assert "Showing 1–20 of 25" in first and "page=2" in first and "q=tech1" in first
    second = listing(client, q="tech1", page=2)
    assert "Showing 21–25 of 25" in second and "Tech2" not in second
    assert listing(client, q="tech1", page="junk")  # a bad page number falls back, never a 500


def test_unknown_filter_values_fall_back_to_defaults(client, manager, techs, vacation):
    book(techs[0], vacation, day(0))
    client.force_login(manager)
    body = listing(client, status="bogus", when="bogus", team="abc", role="nope")
    assert "No requests match" in body  # role "nope" matches nothing, but nothing crashed
    assert "Tech1 Tester" in listing(client, status="bogus", when="bogus")


def test_other_managers_teams_never_appear(client, other_manager, techs, vacation):
    book(techs[0], vacation, day(0))
    client.force_login(other_manager)
    assert "Tech1 Tester" not in listing(client, status="any", when="any")


def test_members_cannot_open_the_page_or_detail(client, techs, vacation):
    request = book(techs[0], vacation, day(0))
    client.force_login(techs[1])
    assert client.get(reverse("approvals")).status_code == 403
    assert client.get(reverse("request-detail", args=[request.pk])).status_code == 403


# ---- the detail page ---------------------------------------------------------------- #
def test_detail_shows_days_and_history(client, manager, techs, vacation):
    request = book(techs[0], vacation, day(0), day(1), actor=manager, comment="Agreed by phone")
    client.force_login(manager)
    body = client.get(reverse("request-detail", args=[request.pk])).content.decode()
    assert "Tech1 Tester" in body and "Agreed by phone" in body
    assert "Booked</strong> by Mona Manager on behalf of Tech1 Tester" in body
    assert "Modify Dates" in body and "Reject This Leave" in body


def test_detail_of_another_managers_request_is_404(client, other_manager, techs, vacation):
    request = book(techs[0], vacation, day(0))
    client.force_login(other_manager)
    assert client.get(reverse("request-detail", args=[request.pk])).status_code == 404


def test_actions_return_to_the_page_they_came_from(client, manager, techs, vacation):
    request = book(techs[0], vacation, day(0))
    client.force_login(manager)
    url = reverse("request-detail", args=[request.pk])
    response = client.post(
        reverse("reject-approved", args=[request.pk]), {"reason": "Needed", "next": url}
    )
    assert response.status_code == 302 and response["Location"] == url


@pytest.mark.parametrize("target", ["https://evil.example/x", "//evil.example/x", "javascript:alert(1)"])
def test_next_cannot_redirect_off_site(client, manager, techs, vacation, target):
    request = book(techs[0], vacation, day(0))
    client.force_login(manager)
    response = client.post(reverse("reject-approved", args=[request.pk]), {"reason": "Needed", "next": target})
    assert response["Location"] == reverse("approvals")


# ---- logging leave on behalf of an employee ------------------------------------------- #
def log_url():
    return reverse("log-leave")


def form(employee, leave_type, start, end=None, **extra):
    return {
        "employee": employee.pk,
        "leave_type": leave_type.pk,
        "start_date": start.isoformat(),
        "end_date": (end or start).isoformat(),
        **extra,
    }


def test_members_cannot_log_leave(client, techs, vacation):
    client.force_login(techs[0])
    assert client.get(log_url()).status_code == 403
    assert client.post(log_url(), form(techs[1], vacation, day(0))).status_code == 403
    assert client.get(reverse("manager-check")).status_code == 403


def test_form_lists_only_my_teams_active_employees(client, manager, other_manager, techs, make_member, roles):
    elsewhere = Team.objects.create(name="Elsewhere", manager=other_manager, region=Region.objects.create(name="Far"))
    outsider = make_member("olga", team_obj=elsewhere, role_obj=elsewhere.resource_roles.first())
    gone = make_member("gina")
    gone.is_active = False
    gone.save()
    client.force_login(manager)
    body = client.get(log_url()).content.decode()
    assert "Tech1 Tester (Technical, Toronto)" in body
    assert outsider.get_full_name() not in body and "Gina Tester" not in body
    assert "Mona Manager (" not in body  # managers book their own leave


def test_a_manager_logs_leave_that_is_approved_and_visible_to_the_employee(client, manager, techs, vacation):
    client.force_login(manager)
    response = client.post(log_url(), form(techs[0], vacation, day(0), day(2), comment="Booked by phone"))
    leave = LeaveRequest.objects.get(user=techs[0])
    assert response.status_code == 302 and response["Location"] == reverse("request-detail", args=[leave.pk])
    assert leave.status == Status.APPROVED and leave.created_by == manager and leave.decided_by == manager
    assert leave.decision_comment == "Booked by phone" and leave.days.count() == 3
    entry = AuditLog.objects.get(action="leave.book", entity_id=leave.pk)
    assert entry.actor == manager and entry.on_behalf_of == techs[0]

    client.force_login(techs[0])
    mine = client.get(reverse("my-leave")).content.decode()
    assert "Logged by Mona Manager" in mine and "Booked by phone" in mine


def test_own_bookings_do_not_say_logged_by(client, techs, vacation):
    book(techs[0], vacation, day(0))
    client.force_login(techs[0])
    assert "Logged by" not in client.get(reverse("my-leave")).content.decode()


def test_past_dates_and_restricted_leave_types_are_allowed_for_managers(client, manager, team, techs):
    restricted = LeaveType.objects.create(team=team, name="Jury duty", employee_bookable=False)
    past = TODAY - dt.timedelta(days=7)  # a Monday, already over
    client.force_login(manager)
    client.post(log_url(), form(techs[0], restricted, past))
    assert LeaveRequest.objects.filter(user=techs[0], start_date=past, status=Status.APPROVED).exists()


def test_floor_breach_is_refused_with_the_numbers_until_overridden(client, manager, team, roles, techs, vacation):
    set_floor(team, roles["Technical"], manager, 2)
    book(techs[1], vacation, day(0))  # leaves exactly the floor
    client.force_login(manager)
    response = client.post(log_url(), form(techs[0], vacation, day(0)))
    body = response.content.decode()
    assert response.status_code == 200 and "Not logged" in body and "floor 2, 1 available" in body
    assert not LeaveRequest.objects.filter(user=techs[0]).exists()

    needs_reason = client.post(log_url(), form(techs[0], vacation, day(0), override_floor="1"))
    assert "Give a reason to override the floor" in needs_reason.content.decode()
    assert not LeaveRequest.objects.filter(user=techs[0]).exists()

    client.post(log_url(), form(techs[0], vacation, day(0), override_floor="1", override_reason="Bereavement"))
    leave = LeaveRequest.objects.get(user=techs[0])
    assert leave.floor_overridden and leave.override_reason == "Bereavement" and leave.status == Status.APPROVED


def test_overlapping_leave_is_explained(client, manager, techs, vacation):
    book(techs[0], vacation, day(0))
    client.force_login(manager)
    body = client.post(log_url(), form(techs[0], vacation, day(0))).content.decode()
    assert "already leave on one or more of these days" in body
    assert LeaveRequest.objects.filter(user=techs[0]).count() == 1


def test_weekend_only_dates_are_explained(client, manager, techs, vacation):
    client.force_login(manager)
    body = client.post(log_url(), form(techs[0], vacation, day(5), day(6))).content.decode()
    assert "no working days" in body
    assert not LeaveRequest.objects.exists()


def test_cannot_log_for_someone_outside_my_teams(client, other_manager, make_member, techs, vacation):
    mine = Team.objects.create(name="Mine", manager=other_manager, region=Region.objects.create(name="Mine"))
    make_member("mia", team_obj=mine, role_obj=mine.resource_roles.first())  # so the form renders
    client.force_login(other_manager)
    response = client.post(log_url(), form(techs[0], vacation, day(0)))
    assert response.status_code == 200 and "Select a valid choice" in response.content.decode()
    assert not LeaveRequest.objects.exists()


def test_leave_type_must_match_the_employees_team(client, manager, other_manager, techs):
    other = Team.objects.create(name="Other", manager=manager, region=Region.objects.create(name="Else"))
    foreign_type = other.leave_types.get(name="Vacation")
    client.force_login(manager)
    body = client.post(log_url(), form(techs[0], foreign_type, day(0))).content.decode()
    assert "belongs to a different team" in body
    assert not LeaveRequest.objects.exists()


def test_a_manager_cannot_be_the_employee(client, manager, techs, vacation):
    client.force_login(manager)
    response = client.post(log_url(), form(manager, vacation, day(0)))
    assert "Select a valid choice" in response.content.decode()


# ---- the live check ------------------------------------------------------------------ #
def check(client, employee, leave_type, start, end):
    return client.get(
        reverse("manager-check"),
        {"employee": employee.pk, "leave_type": leave_type.pk, "start": start.isoformat(), "end": end.isoformat()},
    )


def test_check_reports_cover_and_breaches(client, manager, team, roles, techs, vacation):
    set_floor(team, roles["Technical"], manager, 2)
    book(techs[1], vacation, day(0))
    client.force_login(manager)
    ok = check(client, techs[0], vacation, day(1), day(2)).json()
    assert ok["working_days"] == 2 and ok["violations"] == [] and ok["status"] == Status.APPROVED
    bad = check(client, techs[0], vacation, day(0), day(1)).json()
    assert [v["date"] for v in bad["violations"]] == [day(0).isoformat()]
    assert bad["violations"][0]["floor"] == 2 and bad["violations"][0]["available_after"] == 1


def test_check_validates_input_and_scope(client, manager, other_manager, techs, vacation):
    client.force_login(manager)
    assert client.get(reverse("manager-check"), {"employee": "x"}).status_code == 400
    client.force_login(other_manager)
    assert check(client, techs[0], vacation, day(0), day(1)).status_code == 404


def test_check_allows_past_dates_for_managers(client, manager, techs, vacation):
    client.force_login(manager)
    body = check(client, techs[0], vacation, TODAY - dt.timedelta(days=7), TODAY - dt.timedelta(days=7)).json()
    assert body["problem"] is None and body["working_days"] == 1


# ---- service level ----------------------------------------------------------------------- #
def test_comment_is_ignored_unless_a_manager_books(techs, manager, vacation):
    own = book(techs[0], vacation, day(0), comment="sneaky")
    assert own.decision_comment == ""
    logged = book(techs[1], vacation, day(1), actor=manager, comment="  From the manager ")
    assert logged.decision_comment == "From the manager"


def test_only_the_teams_manager_can_book_on_behalf(techs, other_manager, vacation):
    with pytest.raises(NotAllowed):
        book(techs[0], vacation, day(0), actor=other_manager)
    with pytest.raises(NotAllowed):
        book(techs[0], vacation, day(0), actor=techs[1])


def test_preview_privileged_skips_the_past_and_employee_only_rules(techs, team, vacation):
    restricted = LeaveType.objects.create(team=team, name="Jury duty", employee_bookable=False)
    past = TODAY - dt.timedelta(days=7)
    normal = services.preview_booking(user=techs[0], leave_type=vacation, start_date=past, end_date=past, today=TODAY)
    assert normal.problem
    privileged = services.preview_booking(
        user=techs[0], leave_type=restricted, start_date=past, end_date=past, today=TODAY, privileged=True
    )
    assert privileged.problem is None and privileged.status == Status.APPROVED
