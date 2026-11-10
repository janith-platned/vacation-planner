"""Anonymous counts and the privacy rule: team members never receive colleagues' names."""
import json

import pytest
from django.urls import reverse

from leave import services

from .conftest import TODAY, day

DAY_KEYS = {"date", "headcount", "on_leave", "floor", "slots_left"}


def book(user, leave_type, start, end=None):
    return services.book_leave(user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY)


def get(client, **params):
    params.setdefault("start", day(0).isoformat())
    params.setdefault("end", day(6).isoformat())
    return client.get(reverse("calendar-counts"), params)


@pytest.fixture
def busy_team(team, roles, manager, techs, make_member, vacation):
    """tech2, tech3 and a functional colleague are on leave; the caller is tech1."""
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=1)
    functional = make_member("func1", role="Functional")
    book(techs[1], vacation, day(0))
    book(techs[2], vacation, day(1))
    book(functional, vacation, day(0))
    return techs, functional


# ---- the numbers ------------------------------------------------------------ #
def test_counts_show_headcount_on_leave_floor_and_slots_left(team, roles, busy_team):
    counts = {c.date: c for c in services.role_day_counts(team, roles["Technical"], day(0), day(6))}
    assert (counts[day(0)].headcount, counts[day(0)].on_leave, counts[day(0)].floor, counts[day(0)].slots_left) == (3, 1, 1, 1)
    assert counts[day(2)].on_leave == 0 and counts[day(2)].slots_left == 2
    assert day(5) not in counts  # weekends are not working days


def test_counts_only_include_the_requested_role(team, roles, busy_team):
    functional = {c.date: c for c in services.role_day_counts(team, roles["Functional"], day(0), day(0))}
    assert functional[day(0)].on_leave == 1 and functional[day(0)].floor is None


def test_pending_leave_is_included_in_the_count(team, roles, techs, vacation):
    team.approval_threshold = 0
    team.save()
    book(techs[0], vacation, day(0))
    assert services.role_day_counts(team, roles["Technical"], day(0), day(0))[0].on_leave == 1


# ---- the API ---------------------------------------------------------------- #
def test_member_gets_only_their_own_role_as_anonymous_numbers(client, busy_team):
    techs, functional = busy_team
    client.force_login(techs[0])
    response = get(client)
    assert response.status_code == 200
    payload = response.json()
    assert [r["role"] for r in payload["roles"]] == ["Technical"]
    assert all(set(d) == DAY_KEYS for d in payload["roles"][0]["days"])


def test_member_response_contains_no_colleague_identifiers(client, busy_team, manager):
    techs, functional = busy_team
    client.force_login(techs[0])
    body = get(client).content.decode()
    for other in (techs[1], techs[2], functional, manager):
        for identifier in (other.username, other.first_name, other.last_name, other.get_full_name()):
            if identifier and identifier != techs[0].last_name:  # everyone shares the surname "Tester"
                assert identifier not in body
    assert "email" not in body and "username" not in body
    assert not json.loads(body).get("requests")


def test_member_cannot_ask_for_a_team_view(client, team, busy_team):
    techs, _ = busy_team
    client.force_login(techs[0])
    assert get(client, team=team.id).status_code == 403


def test_manager_sees_every_role_but_still_only_numbers(client, team, busy_team, manager):
    client.force_login(manager)
    response = get(client, team=team.id)
    assert response.status_code == 200
    assert {r["role"] for r in response.json()["roles"]} == {"Functional", "Technical", "Systems"}
    assert "tech2" not in response.content.decode()


def test_other_teams_manager_is_refused_and_admin_is_allowed(client, team, other_manager, admin_user):
    client.force_login(other_manager)
    assert get(client, team=team.id).status_code == 403
    client.force_login(admin_user)
    assert get(client, team=team.id).status_code == 200


def test_anonymous_visitors_are_redirected_to_sign_in(client):
    assert get(client).status_code == 302


def test_bad_input_is_rejected(client, techs):
    client.force_login(techs[0])
    assert client.get(reverse("calendar-counts")).status_code == 400
    assert get(client, start="2026-12-10", end="2026-12-01").status_code == 400
    assert get(client, start="2026-01-01", end="2026-12-31").status_code == 400
    assert get(client, team="abc").status_code == 400
