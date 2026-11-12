"""First round of user-testing fixes: pagination everywhere, who approved, mobile layout hooks."""
import datetime as dt
import re

import pytest
from django.urls import reverse

from accounts.models import Department, Region, SystemRole, Team, User
from leave import services
from leave.models import Floor, Holiday, LeaveRequest, LeaveType

from .conftest import PASSWORD, TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")
Status = LeaveRequest.Status


def make_requests(user, leave_type, n, status=Status.APPROVED, start=dt.date(2027, 1, 4)):
    LeaveRequest.objects.bulk_create(
        [
            LeaveRequest(
                user=user, team=user.team, leave_type=leave_type, working_days=1, status=status, created_by=user,
                start_date=start + dt.timedelta(days=7 * i), end_date=start + dt.timedelta(days=7 * i),
            )
            for i in range(n)
        ]
    )


def count_rows(body, marker):
    return body.count(marker)


# ---- pagination ------------------------------------------------------------------------------------ #
def test_my_leave_is_paged(client, techs, vacation):
    make_requests(techs[0], vacation, 40)
    client.force_login(techs[0])
    first = client.get(reverse("my-leave")).content.decode()
    assert "Showing 1–15 of 40" in first and "page=2" in first
    assert first.count("<tr>") == 1 + 15  # header + 15 rows
    third = client.get(reverse("my-leave"), {"page": 3}).content.decode()
    assert "Showing 31–40 of 40" in third
    assert client.get(reverse("my-leave"), {"page": "junk"}).status_code == 200


def test_members_list_is_paged_and_keeps_filters(client, manager, team, roles, make_member):
    for i in range(60):
        make_member(f"p{i:02d}")
    client.force_login(manager)
    body = client.get(reverse("members"), {"q": "tester"}).content.decode()
    assert "Showing 1–25 of 60" in body and "q=tester" in body and "page=2" in body
    assert "Showing 51–60 of 60" in client.get(reverse("members"), {"q": "tester", "page": 3}).content.decode()


def test_breaches_are_paged(client, manager, team, roles, techs, vacation):
    # 21 Mondays inside the 180-day window, each with two of three technicals off
    for i in range(21):
        services.book_leave(user=techs[0], leave_type=vacation, start_date=day(0) + dt.timedelta(days=7 * i), end_date=day(0) + dt.timedelta(days=7 * i), today=TODAY)
        services.book_leave(user=techs[1], leave_type=vacation, start_date=day(0) + dt.timedelta(days=7 * i), end_date=day(0) + dt.timedelta(days=7 * i), today=TODAY)
    for month in (12, 1, 2, 3, 4):
        year = 2026 if month == 12 else 2027
        services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=year, month=month, min_available=3)
    client.force_login(manager)
    body = client.get(reverse("breaches"), {"days": 180}).content.decode()
    assert "Showing 1–20 of 21" in body and "21</strong> role-day" in body
    assert "Showing 21–21 of 21" in client.get(reverse("breaches"), {"days": 180, "page": 2}).content.decode()


def test_console_lists_are_paged_independently(client, admin_user, manager):
    region = Region.objects.create(name="R0")
    for i in range(18):
        Team.objects.create(name=f"Team {i:02d}", manager=manager, region=region)
    for i in range(12):
        Region.objects.create(name=f"Region {i:02d}")
        Department.objects.create(name=f"Dept {i:02d}")
    for i in range(17):
        User.objects.create_user(f"mgr{i}", password=PASSWORD, system_role=SystemRole.MANAGER, must_change_password=False)
    client.force_login(admin_user)
    body = client.get(reverse("console")).content.decode()
    assert "Showing 1–15 of 18" in body  # teams
    assert "rpage=2" in body and "dpage=2" in body and "mpage=2" in body
    second_teams = client.get(reverse("console"), {"page": 2}).content.decode()
    assert "Showing 16–18 of 18" in second_teams and "Team 17" in second_teams
    # paging one list leaves the others where they are
    assert "Region 00" in client.get(reverse("console"), {"page": 2}).content.decode()
    # the region pickers still offer every region, not just the visible page
    assert "Region 11" in body.split('id="f-region"')[1].split("</select>")[0]


def test_settings_lists_are_paged(client, manager, team, vacation):
    for i in range(45):
        Holiday.objects.create(team=team, date=dt.date(2026, 1, 1) + dt.timedelta(days=i * 2), name=f"Holiday {i}")
    for i in range(20):
        LeaveType.objects.create(team=team, name=f"Type {i:02d}")
    client.force_login(manager)
    body = client.get(reverse("settings"), {"year": 2026}).content.decode()
    assert "Showing 1–20 of 45" in body and "hpage=2" in body
    assert "lpage=2" in body  # 21 leave types, 15 per page
    assert "Showing 21–40 of 45" in client.get(reverse("settings"), {"year": 2026, "hpage": 2}).content.decode()


def test_member_detail_and_floor_overrides_are_paged(client, manager, team, roles, techs, vacation):
    make_requests(techs[0], vacation, 25, start=dt.date(2026, 12, 7))
    for i in range(14):
        services.set_floor_override(actor=manager, team=team, resource_role=roles["Technical"], start_date=day(i), end_date=day(i), min_available=1)
    client.force_login(manager)
    detail = client.get(reverse("member-detail", args=[techs[0].pk])).content.decode()
    assert "Showing 1–10 of 25" in detail
    floors = client.get(reverse("floors"), {"month": "2026-12"}).content.decode()
    assert "Showing 1–10 of 14" in floors and "opage=2" in floors


def test_pending_cards_are_paged_but_all_are_counted(client, manager, team, techs, vacation):
    make_requests(techs[0], vacation, 13, status=Status.PENDING)
    client.force_login(manager)
    body = client.get(reverse("approvals")).content.decode()
    assert "13 requests waiting" in body and body.count('class="card request"') == 10
    assert "ppage=2" in body
    assert client.get(reverse("approvals"), {"ppage": 2}).content.decode().count('class="card request"') == 3


# ---- who approved ------------------------------------------------------------------------------------ #
def test_approved_leave_shows_who_and_when(client, manager, team, techs, vacation):
    team.approval_threshold = 0
    team.save()
    decided = services.book_leave(user=techs[0], leave_type=vacation, start_date=day(0), end_date=day(0), today=TODAY)
    services.decide_request(request=decided, actor=manager, approve=True)
    rejected = services.book_leave(user=techs[0], leave_type=vacation, start_date=day(1), end_date=day(1), today=TODAY)
    services.decide_request(request=rejected, actor=manager, approve=False, comment="No")
    team.approval_threshold = 5
    team.save()
    auto = services.book_leave(user=techs[0], leave_type=vacation, start_date=day(2), end_date=day(2), today=TODAY)
    logged = services.book_leave(user=techs[0], leave_type=vacation, start_date=day(3), end_date=day(3), actor=manager, today=TODAY)
    client.force_login(techs[0])
    mine = client.get(reverse("my-leave")).content.decode()
    decided.refresh_from_db()
    rejected.refresh_from_db()
    assert f"Approved by Mona Manager on {decided.decided_at.day} {decided.decided_at:%b %Y}" in mine
    assert f"Rejected by Mona Manager on {rejected.decided_at.day} {rejected.decided_at:%b %Y}" in mine
    assert "Approved automatically" in mine
    assert mine.count("Approved by Mona Manager") == 2  # the approval and the one the manager logged
    assert auto.decided_by is None and logged.decided_by == manager


def test_approver_shown_on_detail_and_in_the_table(client, manager, team, techs, vacation):
    team.approval_threshold = 0
    team.save()
    request = services.book_leave(user=techs[0], leave_type=vacation, start_date=day(0), end_date=day(0), today=TODAY)
    services.decide_request(request=request, actor=manager, approve=True)
    client.force_login(manager)
    assert "by Mona Manager on" in client.get(reverse("request-detail", args=[request.pk])).content.decode()
    table = client.get(reverse("approvals")).content.decode().split("All requests")[1]
    assert "Approved by Mona Manager on" in table


def test_pending_leave_shows_no_approver(client, manager, team, techs, vacation):
    team.approval_threshold = 0
    team.save()
    services.book_leave(user=techs[0], leave_type=vacation, start_date=day(0), end_date=day(0), today=TODAY)
    client.force_login(techs[0])
    body = client.get(reverse("my-leave")).content.decode()
    assert "Pending" in body and "Approved by" not in body and "Approved automatically" not in body


# ---- mobile layout hooks ------------------------------------------------------------------------------- #
def test_navigation_has_an_accessible_menu_button(client, techs):
    client.force_login(techs[0])
    body = client.get(reverse("my-leave")).content.decode()
    button = re.search(r'<button type="button" class="nav-toggle"[^>]*>', body).group(0)
    assert 'aria-expanded="false"' in button and 'aria-controls="main-nav"' in button
    assert 'id="main-nav"' in body and "Menu</span>" in body
    assert "Escape" in body  # closes with the keyboard


def test_calendar_cells_have_compact_text_for_narrow_screens_and_full_text_for_readers(client, techs, manager, team, roles, vacation):
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=2)
    services.book_leave(user=techs[0], leave_type=vacation, start_date=day(0), end_date=day(0), today=TODAY)
    client.force_login(techs[0])
    body = client.get(reverse("calendar"), {"month": "2026-12"}).content.decode()
    assert '<span class="short" aria-hidden="true">1/3 off</span>' in body
    assert '<span class="short" aria-hidden="true">1 left</span>' in body
    assert '<span class="short" aria-hidden="true">Off</span>' in body  # weekends
    assert '<span class="long">1 of 3 off</span>' in body  # the full wording stays in the page
    assert '<span class="short" aria-hidden="true">You</span>' in body


def test_tables_label_their_cells_for_the_stacked_mobile_layout(client, manager, admin_user, team, techs, vacation):
    make_requests(techs[0], vacation, 2)
    services.set_month_floor(actor=manager, team=team, resource_role=team.resource_roles.first(), year=2026, month=12, min_available=1)
    for user, name in [(techs[0], "my-leave"), (manager, "approvals"), (manager, "members"), (manager, "audit"), (admin_user, "console")]:
        client.force_login(user)
        body = client.get(reverse(name), {"status": "any", "when": "any"}).content.decode()
        rows = re.findall(r"<tr>(.*?)</tr>", body.split("<tbody>")[1].split("</tbody>")[0], flags=re.S) if "<tbody>" in body else []
        assert rows, name
        for row in rows:
            for attrs in re.findall(r"<td([^>]*)>", row):
                # every data cell says what it is when the table is stacked, except the action cell
                assert "data-label" in attrs or 'class="act"' in attrs or 'class="muted"' in attrs, (name, attrs)
