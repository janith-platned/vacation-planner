"""The Settings screen: team rules, roles, leave types and holidays."""
import datetime as dt

import pytest
from django.urls import reverse

from accounts.models import Region, ResourceRole, Team
from leave import services
from leave.models import AuditLog, Holiday, LeaveRequest, LeaveType

from .conftest import TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")


def post(client, name, data, *args):
    return client.post(reverse(name, args=args), data, follow=True).content.decode()


def rules(team, **over):
    data = {
        "team": team.pk, "approval_mode": "threshold", "approval_threshold": "5",
        "working_days": ["0", "1", "2", "3", "4"], "timezone": "America/New_York",
    }
    data.update(over)
    return data


# ---- access ------------------------------------------------------------------------------ #
def test_members_get_403_and_actions_need_post(client, techs, manager, team):
    client.force_login(techs[0])
    assert client.get(reverse("settings")).status_code == 403
    assert client.post(reverse("save-rules"), rules(team)).status_code == 403
    assert client.post(reverse("add-holiday")).status_code == 403
    client.force_login(manager)
    for name in ("save-rules", "add-role", "save-leave-type", "add-holiday"):
        assert client.get(reverse(name)).status_code == 405


def test_page_renders_all_sections(client, manager, team, vacation):
    client.force_login(manager)
    body = client.get(reverse("settings")).content.decode()
    for text in ("Team rules", "Resource roles", "Leave types", "Holidays", "Vacation", "Notifications"):
        assert text in body


def test_manager_without_teams_sees_empty_state(client, other_manager):
    client.force_login(other_manager)
    assert "don't manage any teams" in client.get(reverse("settings")).content.decode()


# ---- team rules --------------------------------------------------------------------------- #
def test_rules_are_saved_and_audited(client, manager, team):
    client.force_login(manager)
    body = post(client, "save-rules", rules(team, approval_threshold="3", working_days=["0", "1", "2", "3"],
                                            timezone="America/Toronto", manager_counts="1"))
    team.refresh_from_db()
    assert "Team rules saved" in body
    assert (team.approval_threshold, team.working_days, team.timezone, team.manager_counts_toward_headcount) == (
        3, [0, 1, 2, 3], "America/Toronto", True)
    assert AuditLog.objects.filter(action="team.settings", actor=manager, entity_id=team.pk).exists()


def test_never_needs_approval_and_zero_means_everything(client, manager, team):
    client.force_login(manager)
    post(client, "save-rules", rules(team, approval_mode="never"))
    team.refresh_from_db()
    assert team.approval_threshold is None
    post(client, "save-rules", rules(team, approval_threshold="0"))
    team.refresh_from_db()
    assert team.approval_threshold == 0


def test_new_threshold_takes_effect_on_booking(client, manager, team, techs, vacation):
    client.force_login(manager)
    post(client, "save-rules", rules(team, approval_threshold="1"))
    two_days = services.book_leave(user=techs[0], leave_type=vacation, start_date=day(0), end_date=day(1), today=TODAY)
    assert two_days.status == LeaveRequest.Status.PENDING


def test_bad_rules_are_refused_and_nothing_changes(client, manager, team):
    client.force_login(manager)
    for bad, message in [
        (rules(team, working_days=[]), "non-empty list"),
        (rules(team, timezone="Mars/Olympus"), "Unknown time zone"),
        (rules(team, approval_threshold="9999"), "between 0 and 365"),
        (rules(team, approval_threshold="abc"), "whole numbers"),
        (rules(team, working_days=["9"]), "0-6"),
    ]:
        assert message in post(client, "save-rules", bad)
    team.refresh_from_db()
    assert team.approval_threshold == 5 and team.working_days == [0, 1, 2, 3, 4] and team.timezone == "America/New_York"


def test_changing_working_days_does_not_move_booked_leave(client, manager, team, techs, vacation):
    booking = services.book_leave(user=techs[0], leave_type=vacation, start_date=day(0), end_date=day(1), today=TODAY)
    client.force_login(manager)
    post(client, "save-rules", rules(team, working_days=["2", "3", "4"]))
    booking.refresh_from_db()
    assert booking.days.count() == 2 and booking.working_days == 2


def test_cannot_change_another_managers_team(client, other_manager, team):
    mine = Team.objects.create(name="Mine", manager=other_manager, region=Region.objects.create(name="M"))
    client.force_login(other_manager)
    post(client, "save-rules", rules(team, approval_threshold="1"))  # Toronto is not theirs
    team.refresh_from_db()
    mine.refresh_from_db()
    assert team.approval_threshold == 5 and mine.approval_threshold == 1  # fell back to their own team


# ---- holidays ------------------------------------------------------------------------------ #
def test_holiday_removes_a_working_day_and_can_be_deleted(client, manager, team, techs, vacation):
    client.force_login(manager)
    body = post(client, "add-holiday", {"team": team.pk, "date": day(1).isoformat(), "name": "Founders Day"})
    assert "Holiday added on Tue 8 Dec 2026" in body and "Founders Day" in body
    assert services.working_days(team, day(0), day(2)) == [day(0), day(2)]
    holiday = Holiday.objects.get(team=team)
    assert "Holiday removed" in post(client, "delete-holiday", {}, holiday.pk)
    assert services.working_days(team, day(0), day(2)) == [day(0), day(1), day(2)]


def test_holiday_validation(client, manager, team):
    client.force_login(manager)
    post(client, "add-holiday", {"team": team.pk, "date": day(1).isoformat(), "name": "A"})
    assert "already a holiday on that date" in post(client, "add-holiday", {"team": team.pk, "date": day(1).isoformat(), "name": "B"})
    assert "Enter a valid date" in post(client, "add-holiday", {"team": team.pk, "date": "nope", "name": "C"})
    assert "Give the holiday a name" in post(client, "add-holiday", {"team": team.pk, "date": day(2).isoformat(), "name": " "})
    assert Holiday.objects.count() == 1


def test_other_managers_holidays_are_404(client, manager, other_manager, team):
    holiday = Holiday.objects.create(team=team, date=day(1), name="X")
    client.force_login(other_manager)
    assert client.post(reverse("delete-holiday", args=[holiday.pk])).status_code == 404
    assert Holiday.objects.filter(pk=holiday.pk).exists()


def test_holiday_year_filter(client, manager, team):
    Holiday.objects.create(team=team, date=dt.date(2027, 1, 1), name="New Year 2027")
    client.force_login(manager)
    assert "New Year 2027" not in client.get(reverse("settings"), {"year": 2026}).content.decode()
    assert "New Year 2027" in client.get(reverse("settings"), {"year": 2027}).content.decode()
    assert client.get(reverse("settings"), {"year": "junk"}).status_code == 200


# ---- leave types ---------------------------------------------------------------------------- #
def lt_data(team, **over):
    data = {"team": team.pk, "name": "Jury duty", "counts_toward_floor": "1", "employee_bookable": "1", "approval_threshold_override": "5"}
    data.update(over)
    return data


def test_add_a_leave_type_with_its_own_threshold(client, manager, team, techs):
    client.force_login(manager)
    body = post(client, "save-leave-type", lt_data(team, approval_threshold_override="0", always_needs_approval="1"))
    assert "Leave type added to: Toronto" in body
    jury = LeaveType.objects.get(team=team, name="Jury duty")
    assert jury.approval_threshold_override == 0 and jury.always_needs_approval and jury.active
    booking = services.book_leave(user=techs[0], leave_type=jury, start_date=day(0), end_date=day(0), today=TODAY)
    assert booking.status == LeaveRequest.Status.PENDING


def test_edit_deactivate_and_flags(client, manager, team, techs, vacation):
    client.force_login(manager)
    post(client, "save-leave-type", lt_data(team, id=vacation.pk, name="Annual leave"))  # no active box = switched off
    vacation.refresh_from_db()
    assert vacation.name == "Annual leave" and not vacation.active
    assert vacation.employee_bookable and vacation.counts_toward_floor
    assert vacation.pk not in [lt.pk for lt in LeaveType.objects.filter(team=team, active=True)]
    post(client, "save-leave-type", lt_data(team, id=vacation.pk, name="Annual leave", active="1", counts_toward_floor=""))
    vacation.refresh_from_db()
    assert vacation.active and not vacation.counts_toward_floor
    assert AuditLog.objects.filter(action="leave_type.update", entity_id=vacation.pk).count() == 2


def test_leave_type_validation_and_scope(client, manager, other_manager, team, vacation):
    client.force_login(manager)
    assert "Already has a leave type named" in post(client, "save-leave-type", lt_data(team, name="Vacation"))
    assert "Give the leave type a name" in post(client, "save-leave-type", lt_data(team, name=" "))
    assert "between 0 and 365" in post(client, "save-leave-type", lt_data(team, approval_threshold_override="500"))
    client.force_login(other_manager)
    Team.objects.create(name="Mine", manager=other_manager, region=Region.objects.create(name="M"))
    assert client.post(reverse("save-leave-type"), lt_data(team, id=vacation.pk, name="Hijacked")).status_code == 404


# ---- resource roles ---------------------------------------------------------------------------- #
def test_add_rename_and_switch_off_a_role(client, manager, team, roles):
    client.force_login(manager)
    assert "Role added" in post(client, "add-role", {"team": team.pk, "name": "Support"})
    support = ResourceRole.objects.get(team=team, name="Support")
    assert services.headcount(team, support) == 0
    post(client, "update-role", {"name": "Customer Support", "active": "1"}, support.pk)
    support.refresh_from_db()
    assert support.name == "Customer Support"
    post(client, "update-role", {"name": "Customer Support"}, support.pk)  # unticked = inactive
    support.refresh_from_db()
    assert not support.active


def test_role_with_active_people_cannot_be_switched_off(client, manager, team, roles, techs):
    client.force_login(manager)
    body = post(client, "update-role", {"name": "Technical"}, roles["Technical"].pk)
    assert "still has active people" in body
    roles["Technical"].refresh_from_db()
    assert roles["Technical"].active


def test_role_name_must_be_unique_and_present(client, manager, team, roles):
    client.force_login(manager)
    assert "already has a role" in post(client, "add-role", {"team": team.pk, "name": "Technical"})
    assert "Give the role a name" in post(client, "add-role", {"team": team.pk, "name": " "})
    assert "already has a role" in post(client, "update-role", {"name": "Technical", "active": "1"}, roles["Functional"].pk)


def test_other_managers_roles_are_404(client, other_manager, roles):
    client.force_login(other_manager)
    assert client.post(reverse("update-role", args=[roles["Technical"].pk]), {"name": "X"}).status_code == 404


# ---- service level: the rules hold even if a view forgot to check -------------------------- #
def test_services_refuse_anyone_but_the_teams_manager(team, roles, other_manager, techs, vacation):
    from leave import settings_services as svc
    from leave.exceptions import NotAllowed

    holiday = Holiday.objects.create(team=team, date=day(1), name="X")
    with pytest.raises(NotAllowed):
        svc.delete_holiday(actor=other_manager, holiday=holiday)
    with pytest.raises(NotAllowed):
        svc.add_holiday(actor=techs[0], team=team, date=day(2), name="Y")
    with pytest.raises(NotAllowed):
        svc.add_resource_role(actor=other_manager, team=team, name="Z")
    with pytest.raises(NotAllowed):
        svc.update_resource_role(actor=other_manager, role=roles["Technical"], name="Z", active=True)
    with pytest.raises(NotAllowed):
        svc.save_leave_type(
            actor=other_manager, team=team, leave_type=vacation, name="Z", counts_toward_floor=True,
            always_needs_approval=False, employee_bookable=True, approval_threshold_override=None, active=True,
        )
    with pytest.raises(NotAllowed):
        svc.update_team_rules(
            actor=techs[0], team=team, approval_threshold=1, working_days=[0], timezone="UTC",
            manager_counts_toward_headcount=False,
        )
    assert Holiday.objects.filter(pk=holiday.pk).exists()
