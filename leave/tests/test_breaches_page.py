"""The Breaches screen: what fell below a floor, who is booked, and manual cancellation."""
import pytest
from django.urls import reverse

from accounts.models import Region, Team
from leave import services
from leave.models import AuditLog, LeaveRequest

from .conftest import TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")
Status = LeaveRequest.Status


def book(user, leave_type, start, end=None, **kwargs):
    return services.book_leave(
        user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY, **kwargs
    )


def set_floor(manager, team, role, value):
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=value)


@pytest.fixture
def breached(manager, team, roles, techs, vacation):
    """Tech1 and Tech2 are off on day(0); the floor is then raised to 2 of 3."""
    first = book(techs[0], vacation, day(0), day(1))
    second = book(techs[1], vacation, day(0))
    set_floor(manager, team, roles["Technical"], 2)
    return first, second


def page(client, **params):
    return client.get(reverse("breaches"), params)


def test_lists_the_breached_day_and_who_is_booked(client, manager, breached):
    client.force_login(manager)
    body = page(client).content.decode()
    assert "Mon 7 Dec 2026" in body and "Technical" in body
    assert "Floor 2, 1 available" in body and "short by 1" in body
    assert "Tech1 Tester" in body and "Tech2 Tester" in body
    assert "Tue 8 Dec" not in body  # day(1): only tech1 off, floor holds


def test_no_breaches_is_a_clear_empty_state(client, manager, team, roles, techs, vacation):
    book(techs[0], vacation, day(0))
    set_floor(manager, team, roles["Technical"], 2)  # 2 of 3 available: fine
    client.force_login(manager)
    assert "No days are below their floors" in page(client).content.decode()


def test_look_ahead_window_and_role_filter(client, manager, breached):
    client.force_login(manager)
    assert "Tech1 Tester" not in page(client, days=30).content.decode()  # ends 1 Dec, before the breach
    assert "Tech1 Tester" in page(client, days=60).content.decode()
    assert "Tech1 Tester" in page(client, days="junk").content.decode()  # falls back to 60
    assert "No days are below" in page(client, role="Functional").content.decode()


def test_past_breaches_are_not_listed(client, manager, team, roles, techs, vacation, monkeypatch):
    first = book(techs[0], vacation, day(0))
    book(techs[1], vacation, day(0))
    set_floor(manager, team, roles["Technical"], 2)
    monkeypatch.setattr("leave.services.today_for", lambda t: day(3))  # the week has passed
    client.force_login(manager)
    assert "No days are below" in page(client).content.decode()
    assert first.status == Status.APPROVED


def test_members_are_refused_and_other_managers_see_nothing(client, techs, other_manager, breached):
    client.force_login(techs[2])
    assert page(client).status_code == 403
    client.force_login(other_manager)
    assert "Tech1 Tester" not in page(client).content.decode()


def test_admin_sees_breaches_across_teams_and_can_filter(client, admin_user, breached, team, roles, make_member, vacation):
    other = Team.objects.create(name="Montreal", manager=admin_user, region=Region.objects.create(name="Quebec"))
    role = other.resource_roles.get(name="Technical")
    people = [make_member(f"m{i}", team_obj=other, role_obj=role) for i in range(3)]
    other_vacation = other.leave_types.get(name="Vacation")
    book(people[0], other_vacation, day(0))
    book(people[1], other_vacation, day(0))
    services.set_month_floor(actor=admin_user, team=other, resource_role=role, year=2026, month=12, min_available=2)
    client.force_login(admin_user)
    both = page(client).content.decode()
    assert "Tech1 Tester" in both and "M0 Tester" in both and "Montreal" in both
    only = page(client, team=other.pk).content.decode()
    assert "M0 Tester" in only and "Tech1 Tester" not in only


def test_manager_cancels_with_a_reason_and_the_breach_clears(client, manager, breached, techs):
    first, second = breached
    client.force_login(manager)
    url = reverse("cancel-request", args=[second.pk])
    response = client.post(url, {"reason": "Floor raised for the go-live", "next": reverse("breaches")}, follow=True)
    body = response.content.decode()
    assert "Cancelled Tech2 Tester" in body and "No days are below" in body  # back on the screen, cleared
    second.refresh_from_db()
    assert second.status == Status.CANCELLED_BY_MANAGER and second.cancelled_by == manager
    assert second.cancel_reason == "Floor raised for the go-live" and second.days.count() == 0
    assert AuditLog.objects.filter(action="leave.cancel", entity_id=second.pk, actor=manager).exists()

    client.force_login(techs[1])
    assert "Floor raised for the go-live" in client.get(reverse("my-leave")).content.decode()


def test_cancelling_needs_a_reason(client, manager, breached):
    _, second = breached
    client.force_login(manager)
    body = client.post(reverse("cancel-request", args=[second.pk]), {"reason": "  "}, follow=True).content.decode()
    assert "A reason is required" in body
    second.refresh_from_db()
    assert second.status == Status.APPROVED


def test_cancel_is_post_only_and_scoped(client, manager, other_manager, techs, breached):
    _, second = breached
    url = reverse("cancel-request", args=[second.pk])
    client.force_login(manager)
    assert client.get(url).status_code == 405
    client.force_login(techs[2])
    assert client.post(url, {"reason": "x"}).status_code == 403
    client.force_login(other_manager)
    assert client.post(url, {"reason": "x"}).status_code == 404
    second.refresh_from_db()
    assert second.status == Status.APPROVED


def test_cancel_does_not_redirect_off_site(client, manager, breached):
    _, second = breached
    client.force_login(manager)
    response = client.post(reverse("cancel-request", args=[second.pk]), {"reason": "x", "next": "https://evil.example/"})
    assert response["Location"] == reverse("breaches")


def test_pending_leave_counts_and_can_be_cancelled(client, manager, team, roles, techs, vacation):
    team.approval_threshold = 0
    team.save()
    pending = book(techs[0], vacation, day(0))
    book(techs[1], vacation, day(0), actor=manager)
    set_floor(manager, team, roles["Technical"], 2)
    assert pending.status == Status.PENDING
    client.force_login(manager)
    assert "Pending" in page(client).content.decode()
    client.post(reverse("cancel-request", args=[pending.pk]), {"reason": "Needed on site"})
    pending.refresh_from_db()
    assert pending.status == Status.CANCELLED_BY_MANAGER
