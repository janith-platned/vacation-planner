"""The audit log: team scoping, filters, details, CSV export, and the backfill of old entries."""
import csv
import datetime as dt
import io
from importlib import import_module

import pytest
from django.apps import apps
from django.urls import reverse
from django.utils import timezone

from accounts.models import Region, SystemRole, Team, User
from leave import console_services as console
from leave import services, settings_services
from leave.models import AuditLog, Holiday

from .conftest import PASSWORD, TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")


def book(user, leave_type, start, end=None, **kw):
    return services.book_leave(user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY, **kw)


@pytest.fixture
def other_team(other_manager):
    return Team.objects.create(name="Montreal", manager=other_manager, region=Region.objects.create(name="Quebec"))


@pytest.fixture
def busy(manager, techs, vacation, other_team, other_manager, make_member):
    """Toronto and Montreal both have some history."""
    book(techs[0], vacation, day(0))
    mia = make_member("mia", team_obj=other_team, role_obj=other_team.resource_roles.get(name="Technical"))
    book(mia, other_team.leave_types.get(name="Vacation"), day(0))
    return mia


def page(client, **params):
    return client.get(reverse("audit"), params).content.decode()


# ---- the team is stamped on every entry ------------------------------------------------------- #
def test_entries_carry_the_team_of_what_they_describe(manager, admin_user, team, roles, techs, vacation):
    leave = book(techs[0], vacation, day(0))
    holiday = settings_services.add_holiday(actor=manager, team=team, date=day(1), name="X")
    settings_services.delete_holiday(actor=manager, holiday=holiday)  # the row is gone, the entry keeps its team
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=1)
    region = console.create_region(actor=admin_user, name="Prairies")
    new_team = console.create_team(actor=admin_user, name="Calgary", manager=manager, region=region)
    assert AuditLog.objects.get(action="leave.book", entity_id=leave.pk).team == team
    assert AuditLog.objects.get(action="holiday.delete").team == team
    assert AuditLog.objects.get(action="floor.set_month").team == team
    assert AuditLog.objects.get(action="team.add").team == new_team
    assert AuditLog.objects.get(action="region.add").team is None


# ---- who sees what -------------------------------------------------------------------------------- #
def test_a_manager_sees_only_their_own_teams(client, manager, other_manager, busy):
    client.force_login(manager)
    body = page(client)
    assert "Tech1 Tester" in body and "Mia Tester" not in body
    client.force_login(other_manager)
    body = page(client)
    assert "Mia Tester" in body and "Tech1 Tester" not in body


def test_members_are_refused(client, techs, busy):
    client.force_login(techs[1])
    assert client.get(reverse("audit")).status_code == 403
    assert client.get(reverse("audit-csv")).status_code == 403


def test_admin_sees_everything_including_organisation_wide_changes(client, admin_user, manager, busy):
    console.create_region(actor=admin_user, name="Prairies")
    client.force_login(admin_user)
    body = page(client)
    assert "Tech1 Tester" in body and "Mia Tester" in body and "Region added" in body
    client.force_login(manager)
    assert "Region added" not in page(client)


def test_view_as_narrows_the_admin_to_that_managers_teams(client, admin_user, manager, busy):
    console.create_region(actor=admin_user, name="Prairies")
    client.force_login(admin_user)
    client.post(reverse("view-as-start", args=[manager.pk]))
    body = page(client)
    assert "Tech1 Tester" in body and "Mia Tester" not in body and "Region added" not in body


def test_a_team_filter_cannot_widen_the_scope(client, manager, other_team, busy):
    client.force_login(manager)
    assert "Mia Tester" not in page(client, team=other_team.pk)
    assert "No entries match" in page(client, team=other_team.pk)


# ---- filters, details, paging ---------------------------------------------------------------------- #
def test_filters_by_person_type_and_date(client, manager, team, roles, techs, vacation):
    book(techs[0], vacation, day(0), actor=manager)  # by the manager, for tech1
    book(techs[1], vacation, day(1))  # tech2 books their own
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=1)
    client.force_login(manager)
    assert "Tech2" in page(client, q="tech2") and "Month floor set" not in page(client, q="tech2")
    by_manager = page(client, q="mona")
    assert "Leave booked" in by_manager and "Tech2 Tester" not in by_manager.split("<tbody>")[1]
    floors = page(client, category="floor")
    assert "Month floor set" in floors and "Leave booked" not in floors
    assert "Leave booked" in page(client, category="leave") and "Month floor set" not in page(client, category="leave")
    today = timezone.now().date()
    assert "Leave booked" in page(client, **{"from": today.isoformat(), "to": today.isoformat()})
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    assert "No entries match" in page(client, **{"from": tomorrow})
    assert "Leave booked" in page(client, **{"from": "junk", "to": "junk"})  # bad dates are ignored


def test_details_describe_what_changed(client, manager, team, roles, techs, vacation):
    leave = book(techs[0], vacation, day(0), day(1), actor=manager)
    services.revoke_approved_leave(request=leave, actor=manager, reason="Needed", today=TODAY)
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=2)
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=3)
    client.force_login(manager)
    body = page(client)
    assert "2026-12-07 to 2026-12-08, 2 working days" in body
    assert "approved → rejected" in body
    assert "2026-12: 2 → 3" in body
    assert "Tech1 Tester: Mon 7 Dec 2026 to Tue 8 Dec 2026" in body
    assert "for Tech1 Tester" in body  # booked on their behalf


def test_pagination_keeps_filters(client, manager, team):
    AuditLog.objects.bulk_create(
        [AuditLog(actor=manager, team=team, action="holiday.add", entity_type="Holiday", entity_id=i, after={"date": "2026-12-25", "name": f"H{i}"}) for i in range(120)]
    )
    client.force_login(manager)
    first = page(client, category="settings")
    assert "Showing 1–50 of 120" in first and "page=2" in first and "category=settings" in first
    assert "Showing 101–120 of 120" in page(client, category="settings", page=3)
    assert page(client, page="junk")  # falls back, no 500


# ---- CSV -------------------------------------------------------------------------------------------- #
def rows_of(response):
    return list(csv.reader(io.StringIO(response.content.decode())))


def test_csv_export_respects_scope_and_filters(client, manager, other_manager, team, roles, techs, vacation, busy):
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=1)
    client.force_login(manager)
    response = client.get(reverse("audit-csv"))
    assert response["Content-Type"].startswith("text/csv") and "attachment" in response["Content-Disposition"]
    rows = rows_of(response)
    assert rows[0][:6] == ["When (UTC)", "Team", "Actor", "On behalf of", "Action", "About"]
    text = response.content.decode()
    assert "Tech1 Tester" in text and "Mia Tester" not in text and "Montreal" not in text
    only_floors = rows_of(client.get(reverse("audit-csv"), {"category": "floor"}))
    assert len(only_floors) == 2 and only_floors[1][4] == "Month floor set"
    assert "month" in only_floors[1][8]  # the raw after-state is included for managers


def test_csv_cells_cannot_run_as_formulas(client, manager, team, techs, vacation):
    evil = User.objects.create_user("evil", password=PASSWORD, system_role=SystemRole.MANAGER, first_name="=HYPERLINK(1)", must_change_password=False)
    team.manager = evil
    team.save()
    services.set_month_floor(actor=evil, team=team, resource_role=team.resource_roles.first(), year=2026, month=12, min_available=1)
    client.force_login(evil)
    rows = rows_of(client.get(reverse("audit-csv")))
    assert rows[1][2] == "'=HYPERLINK(1)"


# ---- immutability and backfill ---------------------------------------------------------------------- #
def test_entries_cannot_be_changed_or_deleted(manager, team):
    from django.core.exceptions import ValidationError

    entry = AuditLog.objects.create(actor=manager, team=team, action="holiday.add", entity_type="Holiday", entity_id=1)
    entry.action = "tampered"
    with pytest.raises(ValidationError):
        entry.save()
    with pytest.raises(ValidationError):
        entry.delete()


def test_backfill_assigns_teams_to_old_entries(manager, team, roles, techs, vacation):
    leave = book(techs[0], vacation, day(0))
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=1)
    old = AuditLog.objects.filter(team=team)
    expected = {(e.action, e.entity_id) for e in old}
    AuditLog.objects.update(team=None)  # how entries looked before the column existed
    import_module("leave.migrations.0004_backfill_auditlog_team").backfill(apps, None)
    assert {(e.action, e.entity_id) for e in AuditLog.objects.filter(team=team)} == expected
    assert AuditLog.objects.filter(team__isnull=True).count() == 0
