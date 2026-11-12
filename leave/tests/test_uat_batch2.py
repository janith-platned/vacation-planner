"""Second round of user-testing fixes: leave types for several teams, deleting unused ones, a required approval limit."""
import pytest
from django.urls import reverse

from accounts.models import Region, Team
from leave import services, settings_services as svc
from leave.exceptions import InvalidRequest, NotAllowed
from leave.models import AuditLog, LeaveRequest, LeaveType

from .conftest import TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")
Status = LeaveRequest.Status


@pytest.fixture
def second_team(manager):
    return Team.objects.create(name="Montreal", manager=manager, region=Region.objects.create(name="Quebec"))


@pytest.fixture
def foreign_team(other_manager):
    return Team.objects.create(name="Elsewhere", manager=other_manager, region=Region.objects.create(name="Far"))


def post(client, name, data, *args):
    return client.post(reverse(name, args=args), data, follow=True).content.decode()


def new_type(team, *teams, **over):
    data = {"team": team.pk, "name": "Parental", "counts_toward_floor": "1", "employee_bookable": "1",
            "approval_threshold_override": "3", "teams": [str(t.pk) for t in teams]}
    data.update(over)
    return data


# ---- one leave type for several teams ---------------------------------------------------------------- #
def test_a_leave_type_is_created_in_every_selected_team(client, manager, team, second_team):
    client.force_login(manager)
    body = post(client, "save-leave-type", new_type(team, team, second_team))
    assert "Leave type added to:" in body and "Toronto" in body and "Montreal" in body
    for t in (team, second_team):
        lt = LeaveType.objects.get(team=t, name="Parental")
        assert lt.approval_threshold_override == 3 and lt.active and lt.counts_toward_floor and lt.employee_bookable
    assert AuditLog.objects.filter(action="leave_type.add", actor=manager).count() == 2


def test_no_team_picked_means_the_current_team(client, manager, team, second_team):
    client.force_login(manager)
    post(client, "save-leave-type", new_type(team))
    assert LeaveType.objects.filter(name="Parental").count() == 1
    assert LeaveType.objects.get(name="Parental").team == team


def test_it_is_all_or_nothing_and_names_the_clashing_teams(client, manager, team, second_team):
    LeaveType.objects.create(team=second_team, name="Parental")
    client.force_login(manager)
    body = post(client, "save-leave-type", new_type(team, team, second_team))
    assert "Already has a leave type named &quot;Parental&quot;: Montreal. Nothing was created." in body
    assert not LeaveType.objects.filter(team=team, name="Parental").exists()
    # a different name goes through
    assert "Leave type added to: Toronto, Montreal" in post(client, "save-leave-type", new_type(team, team, second_team, name="Other")) or LeaveType.objects.filter(name="Other").count() == 2


def test_teams_the_manager_does_not_manage_are_ignored(client, manager, team, foreign_team):
    client.force_login(manager)
    post(client, "save-leave-type", new_type(team, team, foreign_team))
    assert LeaveType.objects.filter(name="Parental").count() == 1
    assert not LeaveType.objects.filter(team=foreign_team, name="Parental").exists()


def test_the_service_refuses_teams_the_actor_does_not_manage(manager, team, foreign_team):
    with pytest.raises(NotAllowed):
        svc.create_leave_type(actor=manager, teams=[team, foreign_team], name="X", counts_toward_floor=True,
                              always_needs_approval=False, employee_bookable=True, approval_threshold_override=1)
    assert not LeaveType.objects.filter(name="X").exists()


def test_multi_team_dropdown_only_appears_with_several_teams(client, manager, team):
    client.force_login(manager)
    assert 'name="teams"' not in client.get(reverse("settings")).content.decode()
    Team.objects.create(name="Montreal", manager=manager, region=Region.objects.create(name="Quebec"))
    body = client.get(reverse("settings")).content.decode()
    assert body.count('name="teams"') == 2 and "selected</summary>" in body
    import re

    boxes = re.findall(r'<input type="checkbox" name="teams" value="\d+"( checked)?>', body)
    assert len(boxes) == 2 and sum(1 for b in boxes if b) == 1  # exactly the current team starts ticked


# ---- the approval limit is always a number ------------------------------------------------------------ #
def test_a_blank_limit_is_refused(client, manager, team):
    client.force_login(manager)
    body = post(client, "save-leave-type", new_type(team, team, approval_threshold_override=""))
    assert "Enter the number of working days" in body
    assert not LeaveType.objects.filter(name="Parental").exists()


def test_zero_means_every_request_of_the_type_needs_approval(client, manager, team, techs):
    client.force_login(manager)
    post(client, "save-leave-type", new_type(team, team, approval_threshold_override="0"))
    lt = LeaveType.objects.get(name="Parental")
    request = services.book_leave(user=techs[0], leave_type=lt, start_date=day(0), end_date=day(0), today=TODAY)
    assert request.status == Status.PENDING


def test_the_box_is_prefilled_with_the_teams_limit_and_has_no_misleading_placeholder(client, manager, team, vacation):
    client.force_login(manager)
    body = client.get(reverse("settings")).content.decode()
    assert 'name="approval_threshold_override" value="5"' in body
    assert 'placeholder="team"' not in body
    team.approval_threshold = None
    team.save()
    body = client.get(reverse("settings")).content.decode()
    assert 'name="approval_threshold_override" value="365"' in body and "effectively never" in body


def test_an_existing_types_own_limit_is_shown(client, manager, team, vacation):
    vacation.approval_threshold_override = 2
    vacation.save()
    client.force_login(manager)
    assert 'name="approval_threshold_override" value="2"' in client.get(reverse("settings")).content.decode()


def test_the_team_limit_can_be_pushed_to_every_leave_type(client, manager, team, vacation):
    other = LeaveType.objects.create(team=team, name="Study", approval_threshold_override=9)
    rules = {"team": team.pk, "approval_mode": "threshold", "approval_threshold": "2",
             "working_days": ["0", "1", "2", "3", "4"], "timezone": "America/New_York"}
    client.force_login(manager)
    post(client, "save-rules", rules)
    other.refresh_from_db()
    assert other.approval_threshold_override == 9  # untouched unless asked
    post(client, "save-rules", {**rules, "apply_to_leave_types": "1"})
    other.refresh_from_db()
    vacation.refresh_from_db()
    assert other.approval_threshold_override == 2 and vacation.approval_threshold_override == 2


def test_editing_requires_a_number_too(client, manager, team, vacation):
    client.force_login(manager)
    body = post(client, "save-leave-type", {"team": team.pk, "id": vacation.pk, "name": "Vacation", "active": "1", "approval_threshold_override": ""})
    assert "Enter the number of working days" in body


# ---- deleting ------------------------------------------------------------------------------------------- #
def test_an_unused_leave_type_can_be_deleted(client, manager, team):
    lt = LeaveType.objects.create(team=team, name="Sabbatical", approval_threshold_override=5)
    client.force_login(manager)
    assert "Delete This Leave Type" in client.get(reverse("settings")).content.decode()
    body = post(client, "delete-leave-type", {}, lt.pk)
    assert "Deleted the leave type &quot;Sabbatical&quot;" in body
    assert not LeaveType.objects.filter(pk=lt.pk).exists()
    assert AuditLog.objects.filter(action="leave_type.delete", entity_id=lt.pk, actor=manager).exists()


def test_a_used_leave_type_cannot_be_deleted_but_can_be_switched_off(client, manager, team, techs, vacation):
    services.book_leave(user=techs[0], leave_type=vacation, start_date=day(0), end_date=day(0), today=TODAY)
    client.force_login(manager)
    page = client.get(reverse("settings")).content.decode()
    assert "Used by 1 request, so it cannot be deleted" in page and "Delete This Leave Type" not in page
    body = post(client, "delete-leave-type", {}, vacation.pk)
    assert "used by 1 request, so it cannot be deleted" in body
    assert LeaveType.objects.filter(pk=vacation.pk).exists()
    # cancelled requests still count: the history keeps pointing at the type
    LeaveRequest.objects.update(status=Status.CANCELLED_BY_EMPLOYEE)
    assert "cannot be deleted" in post(client, "delete-leave-type", {}, vacation.pk)
    post(client, "save-leave-type", {"team": team.pk, "id": vacation.pk, "name": "Vacation", "approval_threshold_override": "5"})
    vacation.refresh_from_db()
    assert not vacation.active  # switched off by leaving Active unticked


def test_delete_is_post_only_and_scoped(client, manager, other_manager, techs, team, foreign_team):
    lt = LeaveType.objects.create(team=team, name="Sabbatical", approval_threshold_override=5)
    client.force_login(manager)
    assert client.get(reverse("delete-leave-type", args=[lt.pk])).status_code == 405
    client.force_login(techs[0])
    assert client.post(reverse("delete-leave-type", args=[lt.pk])).status_code == 403
    client.force_login(other_manager)
    assert client.post(reverse("delete-leave-type", args=[lt.pk])).status_code == 404
    assert LeaveType.objects.filter(pk=lt.pk).exists()


def test_the_service_refuses_a_non_manager_deleting(team, techs, other_manager):
    lt = LeaveType.objects.create(team=team, name="Sabbatical", approval_threshold_override=5)
    for who in (techs[0], other_manager):
        with pytest.raises(NotAllowed):
            svc.delete_leave_type(actor=who, leave_type=lt)
    assert LeaveType.objects.filter(pk=lt.pk).exists()
