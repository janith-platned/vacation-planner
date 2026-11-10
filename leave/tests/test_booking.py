"""Booking, approval, cancellation and the floor check."""
import datetime as dt

import pytest

from leave import services
from leave.exceptions import (
    FloorViolationError,
    InvalidRequest,
    InvalidState,
    NoWorkingDaysError,
    NotAllowed,
    OverlapError,
)
from leave.models import AuditLog, Holiday, LeaveDay, LeaveRequest, LeaveType
from leave.services import FloorViolation

from .conftest import TODAY, day

Status = LeaveRequest.Status


def book(user, leave_type, start, end=None, **kwargs):
    return services.book_leave(
        user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY, **kwargs
    )


def set_floor(team, role, manager, value):
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=value)


@pytest.fixture
def floor2(team, roles, manager, techs):
    """Three technicians, floor of 2: one person can be off on any given day."""
    set_floor(team, roles["Technical"], manager, 2)


# ---- the floor check ------------------------------------------------------- #
def test_booking_is_accepted_when_the_floor_holds(floor2, techs, vacation):
    request = book(techs[0], vacation, day(0), day(1))
    assert request.status == Status.APPROVED
    assert request.working_days == 2
    assert LeaveDay.objects.filter(request=request).count() == 2
    assert AuditLog.objects.filter(action="leave.book", entity_id=request.pk).exists()


def test_booking_that_would_breach_the_floor_is_rejected(floor2, techs, vacation):
    book(techs[0], vacation, day(0))
    with pytest.raises(FloorViolationError) as exc:
        book(techs[1], vacation, day(0))
    assert exc.value.violations == [FloorViolation(day(0), floor=2, available_after=1)]
    assert LeaveRequest.objects.count() == 1


def test_only_failing_dates_are_reported_and_nothing_is_created(floor2, techs, vacation):
    book(techs[0], vacation, day(0))
    with pytest.raises(FloorViolationError) as exc:
        book(techs[1], vacation, day(0), day(1))
    assert [v.date for v in exc.value.violations] == [day(0)]
    assert not LeaveDay.objects.filter(user=techs[1]).exists()


def test_pending_requests_reserve_capacity(floor2, team, techs, vacation):
    team.approval_threshold = 0
    team.save()
    assert book(techs[0], vacation, day(0)).status == Status.PENDING
    with pytest.raises(FloorViolationError):
        book(techs[1], vacation, day(0))


def test_no_floor_means_unrestricted(team, techs, vacation):
    for tech in techs:
        book(tech, vacation, day(0))
    assert LeaveDay.objects.filter(date=day(0)).count() == 3


def test_override_can_lower_the_floor_for_one_day(floor2, team, roles, manager, techs, vacation):
    services.set_floor_override(actor=manager, team=team, resource_role=roles["Technical"], start_date=day(0), end_date=day(0), min_available=1)
    book(techs[0], vacation, day(0))
    book(techs[1], vacation, day(0))  # 3 - 1 - 1 = 1 >= 1
    with pytest.raises(FloorViolationError):
        book(techs[2], vacation, day(0))


def test_override_can_raise_the_floor_for_one_day(team, roles, manager, techs, vacation):
    set_floor(team, roles["Technical"], manager, 1)
    services.set_floor_override(actor=manager, team=team, resource_role=roles["Technical"], start_date=day(3), end_date=day(3), min_available=3)
    with pytest.raises(FloorViolationError) as exc:
        book(techs[0], vacation, day(2), day(3))
    assert [v.date for v in exc.value.violations] == [day(3)]


def test_roles_are_independent(floor2, make_member, techs, vacation):
    functional = make_member("func1", role="Functional")
    book(techs[0], vacation, day(0))
    book(functional, vacation, day(0))  # no floor on Functional


def test_leave_type_that_ignores_floors_neither_blocks_nor_is_blocked(floor2, team, techs, vacation):
    training = LeaveType.objects.create(team=team, name="Training", counts_toward_floor=False)
    request = book(techs[0], training, day(0))
    assert not LeaveDay.objects.get(request=request).counts_toward_floor
    book(techs[1], vacation, day(0))  # training did not use the slot
    with pytest.raises(FloorViolationError):
        book(techs[2], vacation, day(0))


def test_manager_is_not_part_of_headcount_by_default(floor2, team, roles, manager, techs, vacation):
    manager.team, manager.resource_role = team, roles["Technical"]
    manager.save()
    assert services.headcount(team, roles["Technical"]) == 3
    request = book(manager, vacation, day(0))
    assert not LeaveDay.objects.get(request=request).counts_toward_floor
    book(techs[0], vacation, day(0))  # manager's day did not use a slot


def test_manager_counts_when_the_team_setting_is_on(team, roles, manager, techs):
    team.manager_counts_toward_headcount = True
    team.save()
    manager.team, manager.resource_role = team, roles["Technical"]
    manager.save()
    assert services.headcount(team, roles["Technical"]) == 4


# ---- working days ---------------------------------------------------------- #
def test_weekends_and_holidays_are_not_counted(team, techs, vacation):
    Holiday.objects.create(team=team, date=day(7), name="Boxing Day observed")
    request = book(techs[0], vacation, day(4), day(7))  # Fri, Sat, Sun, Mon (holiday)
    assert request.working_days == 1
    assert list(request.days.values_list("date", flat=True)) == [day(4)]


def test_request_without_working_days_is_invalid(techs, vacation):
    with pytest.raises(NoWorkingDaysError):
        book(techs[0], vacation, day(5), day(6))


def test_custom_working_days(team, techs, vacation):
    team.working_days = [0, 1, 2, 3, 4, 5]  # Saturday is a working day
    team.save()
    assert book(techs[0], vacation, day(4), day(6)).working_days == 2


# ---- hybrid approval ------------------------------------------------------- #
def test_threshold_decides_between_auto_approval_and_manager_approval(techs, vacation):
    five_days = book(techs[0], vacation, day(0), day(4))
    six_days = book(techs[1], vacation, day(0), day(7))
    assert five_days.status == Status.APPROVED
    assert six_days.status == Status.PENDING


def test_threshold_zero_always_needs_approval_and_none_never(team, techs, vacation):
    team.approval_threshold = 0
    team.save()
    assert book(techs[0], vacation, day(0)).status == Status.PENDING
    team.approval_threshold = None
    team.save()
    assert book(techs[1], vacation, day(0), day(21)).status == Status.APPROVED


def test_leave_type_can_override_the_threshold_or_always_need_approval(team, techs, vacation):
    strict = LeaveType.objects.create(team=team, name="Long leave", approval_threshold_override=1)
    always = LeaveType.objects.create(team=team, name="Unpaid", always_needs_approval=True)
    assert book(techs[0], strict, day(0), day(1)).status == Status.PENDING
    assert book(techs[1], always, day(0)).status == Status.PENDING


def test_approving_keeps_days_and_rejecting_releases_them(floor2, team, manager, techs, vacation):
    team.approval_threshold = 0
    team.save()
    first = book(techs[0], vacation, day(0))
    services.decide_request(request=first, actor=manager, approve=True, comment="Enjoy")
    first.refresh_from_db()
    assert first.status == Status.APPROVED and first.decided_by == manager
    assert first.days.count() == 1

    second = book(techs[1], vacation, day(1))
    services.decide_request(request=second, actor=manager, approve=False, comment="Release window")
    second.refresh_from_db()
    assert second.status == Status.REJECTED
    assert second.days.count() == 0
    book(techs[2], vacation, day(1))  # the rejected day is available again


def test_only_the_teams_manager_or_admin_can_decide(team, techs, vacation, other_manager, admin_user):
    team.approval_threshold = 0
    team.save()
    request = book(techs[0], vacation, day(0))
    for intruder in (techs[1], techs[0], other_manager):
        with pytest.raises(NotAllowed):
            services.decide_request(request=request, actor=intruder, approve=True)
    services.decide_request(request=request, actor=admin_user, approve=True)


def test_only_pending_requests_can_be_decided(team, manager, techs, vacation):
    request = book(techs[0], vacation, day(0))  # auto-approved
    with pytest.raises(InvalidState):
        services.decide_request(request=request, actor=manager, approve=True)


# ---- cancellation ---------------------------------------------------------- #
def test_employee_cancels_freely_and_capacity_is_released(floor2, techs, vacation):
    request = book(techs[0], vacation, day(0))
    services.cancel_leave(request=request, actor=techs[0], today=TODAY)
    request.refresh_from_db()
    assert request.status == Status.CANCELLED_BY_EMPLOYEE
    assert request.days.count() == 0
    book(techs[1], vacation, day(0))


def test_leave_that_has_ended_cannot_be_cancelled(techs, vacation):
    request = book(techs[0], vacation, day(0))
    with pytest.raises(InvalidState):
        services.cancel_leave(request=request, actor=techs[0], today=day(1))


def test_members_cannot_cancel_each_others_leave(techs, vacation):
    request = book(techs[0], vacation, day(0))
    with pytest.raises(NotAllowed):
        services.cancel_leave(request=request, actor=techs[1], today=TODAY)


def test_manager_cancellation_needs_a_reason(techs, vacation, manager):
    request = book(techs[0], vacation, day(0))
    with pytest.raises(InvalidRequest):
        services.cancel_leave(request=request, actor=manager, today=TODAY)
    services.cancel_leave(request=request, actor=manager, reason="Customer go-live", today=TODAY)
    request.refresh_from_db()
    assert request.status == Status.CANCELLED_BY_MANAGER
    assert request.cancel_reason == "Customer go-live"


# ---- floor raised after bookings exist -------------------------------------- #
def test_raising_a_floor_keeps_bookings_and_reports_breaches(team, roles, manager, techs, vacation):
    role = roles["Technical"]
    set_floor(team, role, manager, 1)
    book(techs[0], vacation, day(0))
    book(techs[1], vacation, day(0))

    set_floor(team, role, manager, 2)  # now 1 available against a floor of 2

    assert LeaveDay.objects.filter(date=day(0)).count() == 2  # nothing cancelled automatically
    breaches = services.find_breaches(team, day(0), day(0), actor=manager)
    assert len(breaches) == 1
    assert (breaches[0].floor, breaches[0].available) == (2, 1)
    assert set(breaches[0].user_ids) == {techs[0].id, techs[1].id}
    with pytest.raises(FloorViolationError):
        book(techs[2], vacation, day(0))  # new bookings on a breached day are blocked


def test_breach_report_is_for_managers_only(team, techs):
    with pytest.raises(NotAllowed):
        services.find_breaches(team, day(0), day(4), actor=techs[0])


# ---- manager on-behalf bookings and overrides ------------------------------- #
def test_members_cannot_override_the_floor(floor2, techs, vacation):
    book(techs[0], vacation, day(0))
    with pytest.raises(FloorViolationError):
        book(techs[1], vacation, day(0), override_floor=True, override_reason="please")


def test_manager_override_needs_a_reason_and_is_flagged(floor2, techs, vacation, manager):
    book(techs[0], vacation, day(0))
    kwargs = dict(actor=manager)
    with pytest.raises(FloorViolationError):
        book(techs[1], vacation, day(0), **kwargs)
    with pytest.raises(FloorViolationError):
        book(techs[1], vacation, day(0), override_floor=True, **kwargs)
    request = book(techs[1], vacation, day(0), override_floor=True, override_reason="Family emergency", **kwargs)
    assert request.status == Status.APPROVED
    assert request.floor_overridden and request.override_reason == "Family emergency"
    assert request.created_by == manager
    entry = AuditLog.objects.get(action="leave.book", entity_id=request.pk)
    assert entry.actor == manager and entry.on_behalf_of == techs[1]


def test_manager_can_book_in_the_past_but_members_cannot(techs, vacation, manager):
    past = TODAY - dt.timedelta(days=3)  # a Friday
    with pytest.raises(InvalidRequest):
        book(techs[0], vacation, past)
    assert book(techs[0], vacation, past, actor=manager).status == Status.APPROVED


def test_another_teams_manager_cannot_book_for_a_member(techs, vacation, other_manager):
    with pytest.raises(NotAllowed):
        book(techs[0], vacation, day(0), actor=other_manager)


def test_leave_type_rules(team, techs, vacation, other_manager):
    from accounts.models import Region, Team

    other = Team.objects.create(name="Other", manager=other_manager, region=Region.objects.create(name="BC"))
    with pytest.raises(InvalidRequest):
        book(techs[0], other.leave_types.get(name="Vacation"), day(0))
    manager_only = LeaveType.objects.create(team=team, name="Compassionate", employee_bookable=False)
    with pytest.raises(NotAllowed):
        book(techs[0], manager_only, day(0))
    vacation.active = False
    vacation.save()
    with pytest.raises(InvalidRequest):
        book(techs[0], vacation, day(0))


# ---- overlap and edits ------------------------------------------------------- #
def test_a_person_cannot_book_the_same_day_twice(techs, vacation):
    book(techs[0], vacation, day(0), day(1))
    with pytest.raises(OverlapError):
        book(techs[0], vacation, day(1), day(2))


def test_edit_is_validated_against_the_floor_and_rolled_back_on_failure(floor2, techs, vacation):
    mine = book(techs[0], vacation, day(0))
    book(techs[1], vacation, day(1))
    with pytest.raises(FloorViolationError):
        services.modify_leave(request=mine, actor=techs[0], start_date=day(1), end_date=day(1), today=TODAY)
    mine.refresh_from_db()
    assert list(mine.days.values_list("date", flat=True)) == [day(0)]
    assert mine.status == Status.APPROVED


def test_successful_edit_moves_the_days(floor2, techs, vacation):
    mine = book(techs[0], vacation, day(0))
    services.modify_leave(request=mine, actor=techs[0], start_date=day(2), end_date=day(2), today=TODAY)
    mine.refresh_from_db()
    assert list(mine.days.values_list("date", flat=True)) == [day(2)]
    book(techs[1], vacation, day(0))  # the old day was released


def test_edit_can_extend_over_its_own_days(floor2, techs, vacation):
    mine = book(techs[0], vacation, day(0))
    services.modify_leave(request=mine, actor=techs[0], start_date=day(0), end_date=day(1), today=TODAY)
    assert mine.days.count() == 2


def test_edit_restarts_approval(team, techs, vacation):
    team.approval_threshold = 1
    team.save()
    mine = book(techs[0], vacation, day(0))
    assert mine.status == Status.APPROVED
    updated = services.modify_leave(request=mine, actor=techs[0], start_date=day(0), end_date=day(1), today=TODAY)
    assert updated.status == Status.PENDING and updated.decided_by is None


def test_only_active_future_requests_can_be_edited(techs, vacation, manager):
    mine = book(techs[0], vacation, day(0))
    with pytest.raises(NotAllowed):
        services.modify_leave(request=mine, actor=techs[1], start_date=day(1), end_date=day(1), today=TODAY)
    with pytest.raises(InvalidState):
        services.modify_leave(request=mine, actor=techs[0], start_date=day(8), end_date=day(8), today=day(1))
    services.cancel_leave(request=mine, actor=techs[0], today=TODAY)
    with pytest.raises(InvalidState):
        services.modify_leave(request=mine, actor=techs[0], start_date=day(1), end_date=day(1), today=TODAY)
