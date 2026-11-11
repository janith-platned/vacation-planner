"""Managers changing or rejecting leave that is already approved."""
import datetime as dt

import pytest
from django.urls import reverse

from leave import services
from leave.exceptions import FloorViolationError, InvalidState, NotAllowed
from leave.models import AuditLog, LeaveRequest

from .conftest import TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")
Status = LeaveRequest.Status


def book(user, leave_type, start, end=None, **kwargs):
    return services.book_leave(
        user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY, **kwargs
    )


def set_floor(team, role, manager, value):
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=value)


@pytest.fixture
def approved(techs, vacation):
    """Tech1 is approved for Mon and Tue."""
    request = book(techs[0], vacation, day(0), day(1))
    assert request.status == Status.APPROVED
    return request


def modify_url(request):
    return reverse("modify-request", args=[request.pk])


def reject_url(request):
    return reverse("reject-approved", args=[request.pk])


def dates_form(start, end, **extra):
    return {"start_date": start.isoformat(), "end_date": end.isoformat(), **extra}


# ---- the list ------------------------------------------------------------------ #
def test_approved_leave_is_in_the_table_and_the_detail_page_has_both_actions(client, manager, approved):
    client.force_login(manager)
    body = client.get(reverse("approvals")).content.decode()
    assert "Tech1 Tester" in body and "Manage" in body
    detail_url = reverse("request-detail", args=[approved.pk])
    assert detail_url in body
    detail = client.get(detail_url).content.decode()
    assert "Modify dates" in detail and "Reject this leave" in detail
    assert modify_url(approved) in detail and reject_url(approved) in detail


def test_leave_that_has_ended_is_not_in_the_default_view(client, manager, techs, vacation):
    past = book(techs[1], vacation, TODAY - dt.timedelta(days=3), actor=manager)  # a Friday, already over
    client.force_login(manager)
    body = client.get(reverse("approvals")).content.decode()
    assert "No requests match" in body and "Tech2 Tester" not in body
    assert past.status == Status.APPROVED
    past_view = client.get(reverse("approvals"), {"when": "past"}).content.decode()
    assert "Tech2 Tester" in past_view
    detail = client.get(reverse("request-detail", args=[past.pk])).content.decode()
    assert "Modify dates" not in detail  # over: history only


def test_pending_requests_are_not_in_the_default_approved_table(client, manager, team, techs, vacation):
    team.approval_threshold = 0
    team.save()
    book(techs[0], vacation, day(0))
    client.force_login(manager)
    body = client.get(reverse("approvals")).content.decode()
    assert "No requests match" in body and "Cover holds" in body  # shown as a card, not in the table
    assert "Tech1 Tester" in client.get(reverse("approvals"), {"status": "pending"}).content.decode().split("All requests")[1]


# ---- access ---------------------------------------------------------------------- #
def test_members_cannot_modify_or_reject(client, techs, approved):
    client.force_login(techs[1])
    assert client.post(modify_url(approved), dates_form(day(2), day(2))).status_code == 403
    assert client.post(reject_url(approved), {"reason": "no"}).status_code == 403
    approved.refresh_from_db()
    assert approved.status == Status.APPROVED and approved.start_date == day(0)


def test_another_managers_team_is_off_limits(client, other_manager, approved):
    client.force_login(other_manager)
    assert client.post(modify_url(approved), dates_form(day(2), day(2))).status_code == 404
    assert client.post(reject_url(approved), {"reason": "no"}).status_code == 404


def test_both_actions_need_post(client, manager, approved):
    client.force_login(manager)
    assert client.get(modify_url(approved)).status_code == 405
    assert client.get(reject_url(approved)).status_code == 405


# ---- rejecting approved leave ----------------------------------------------------- #
def test_rejecting_needs_a_reason(client, manager, approved):
    client.force_login(manager)
    response = client.post(reject_url(approved), {"reason": "  "}, follow=True)
    assert "A reason is required" in response.content.decode()
    approved.refresh_from_db()
    assert approved.status == Status.APPROVED and approved.days.count() == 2


def test_rejecting_releases_the_days_and_tells_the_employee_why(client, manager, techs, vacation, approved):
    client.force_login(manager)
    response = client.post(reject_url(approved), {"reason": "Customer go-live that week"}, follow=True)
    assert "Rejected Tech1 Tester" in response.content.decode()
    approved.refresh_from_db()
    assert approved.status == Status.REJECTED and approved.decided_by == manager
    assert approved.decision_comment == "Customer go-live that week" and approved.days.count() == 0
    assert AuditLog.objects.filter(action="leave.revoke", entity_id=approved.pk).exists()

    client.force_login(techs[0])
    mine = client.get(reverse("my-leave")).content.decode()
    assert "Rejected" in mine and "Customer go-live that week" in mine


def test_rejected_days_can_be_booked_by_others(team, roles, manager, techs, vacation, approved):
    set_floor(team, roles["Technical"], manager, 2)
    with pytest.raises(FloorViolationError):
        book(techs[1], vacation, day(0))  # tech1 holds the only slot
    services.revoke_approved_leave(request=approved, actor=manager, reason="Needed on site", today=TODAY)
    book(techs[1], vacation, day(0))


def test_only_approved_future_leave_can_be_rejected(manager, techs, vacation, team, approved):
    team.approval_threshold = 0
    team.save()
    pending = book(techs[1], vacation, day(3))
    with pytest.raises(InvalidState):
        services.revoke_approved_leave(request=pending, actor=manager, reason="x", today=TODAY)
    with pytest.raises(InvalidState):
        services.revoke_approved_leave(request=approved, actor=manager, reason="x", today=day(2))  # already over


def test_revoking_is_for_the_teams_manager_only(techs, other_manager, approved, admin_user):
    for intruder in (techs[1], other_manager):
        with pytest.raises(NotAllowed):
            services.revoke_approved_leave(request=approved, actor=intruder, reason="x", today=TODAY)
    services.revoke_approved_leave(request=approved, actor=admin_user, reason="Policy", today=TODAY)


def test_view_refuses_to_reject_pending_or_already_rejected(client, manager, approved):
    client.force_login(manager)
    client.post(reject_url(approved), {"reason": "first"})
    response = client.post(reject_url(approved), {"reason": "second"}, follow=True)
    assert "Only approved leave can be rejected" in response.content.decode()


# ---- modifying approved leave ------------------------------------------------------- #
def test_modifying_moves_the_days_and_leaves_a_note(client, manager, techs, vacation, approved):
    client.force_login(manager)
    response = client.post(
        modify_url(approved), dates_form(day(2), day(3), comment="Moved for the audit week"), follow=True
    )
    assert "Updated Tech1 Tester" in response.content.decode()
    approved.refresh_from_db()
    assert approved.status == Status.APPROVED and approved.working_days == 2
    assert list(approved.days.order_by("date").values_list("date", flat=True)) == [day(2), day(3)]
    assert approved.decision_comment == "Moved for the audit week"
    assert AuditLog.objects.filter(action="leave.modify", entity_id=approved.pk, actor=manager).exists()

    book(techs[1], vacation, day(0))  # the old days were released
    client.force_login(techs[0])
    mine = client.get(reverse("my-leave")).content.decode()
    assert "Moved for the audit week" in mine and "Wed 9 Dec 2026" in mine


def test_modifying_into_a_floor_breach_is_refused_with_the_numbers(client, manager, team, roles, techs, vacation, approved):
    set_floor(team, roles["Technical"], manager, 2)
    book(techs[1], vacation, day(2))  # the day tech1 wants is already used
    client.force_login(manager)
    response = client.post(modify_url(approved), dates_form(day(2), day(2)), follow=True)
    body = response.content.decode()
    assert "would take the role below its floor" in body and "floor 2, 1 available" in body and "Wed 9 Dec 2026" in body
    assert "Override the floor" in body
    approved.refresh_from_db()
    assert approved.start_date == day(0) and approved.days.count() == 2  # nothing changed


def test_a_manager_can_override_the_floor_with_a_reason(client, manager, team, roles, techs, vacation, approved):
    set_floor(team, roles["Technical"], manager, 2)
    book(techs[1], vacation, day(2))
    client.force_login(manager)
    client.post(
        modify_url(approved),
        dates_form(day(2), day(2), override_floor="1", override_reason="Family emergency"),
    )
    approved.refresh_from_db()
    assert approved.start_date == day(2) and approved.floor_overridden
    assert approved.override_reason == "Family emergency" and approved.status == Status.APPROVED
    assert "Floor override" in client.get(reverse("approvals")).content.decode()


def test_override_without_a_reason_is_refused(client, manager, team, roles, techs, vacation, approved):
    set_floor(team, roles["Technical"], manager, 2)
    book(techs[1], vacation, day(2))
    client.force_login(manager)
    response = client.post(modify_url(approved), dates_form(day(2), day(2), override_floor="1"), follow=True)
    assert "Give a reason to override the floor" in response.content.decode()
    approved.refresh_from_db()
    assert approved.start_date == day(0)


def test_bad_dates_are_explained(client, manager, approved):
    client.force_login(manager)
    backwards = client.post(modify_url(approved), dates_form(day(3), day(1)), follow=True)
    assert "cannot be before the start date" in backwards.content.decode()
    weekend = client.post(modify_url(approved), dates_form(day(5), day(6)), follow=True)
    assert "no working days" in weekend.content.decode()
    approved.refresh_from_db()
    assert approved.start_date == day(0)


def test_only_approved_leave_can_be_modified_from_here(client, manager, team, techs, vacation):
    team.approval_threshold = 0
    team.save()
    pending = book(techs[0], vacation, day(0))
    client.force_login(manager)
    response = client.post(modify_url(pending), dates_form(day(2), day(2)), follow=True)
    assert "Only approved leave can be modified" in response.content.decode()
    pending.refresh_from_db()
    assert pending.status == Status.PENDING and pending.start_date == day(0)


def test_comment_is_only_stored_for_manager_edits(techs, vacation, manager, approved):
    services.modify_leave(request=approved, actor=techs[0], start_date=day(2), end_date=day(2), comment="sneaky", today=TODAY)
    approved.refresh_from_db()
    assert approved.decision_comment == ""  # the owner cannot leave a manager's note
    services.modify_leave(request=approved, actor=manager, start_date=day(3), end_date=day(3), comment="Official", today=TODAY)
    approved.refresh_from_db()
    assert approved.decision_comment == "Official" and approved.status == Status.APPROVED


def test_owner_edits_still_restart_approval(team, techs, vacation, approved):
    team.approval_threshold = 1
    team.save()
    updated = services.modify_leave(
        request=approved, actor=techs[0], start_date=day(0), end_date=day(2), today=TODAY
    )
    assert updated.status == Status.PENDING
