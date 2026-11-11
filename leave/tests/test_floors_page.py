"""The Floors screen: month floors in one step, date overrides, and the grid."""
import pytest
from django.urls import reverse

from accounts.models import Region, Team
from leave import services
from leave.models import AuditLog, Floor

from .conftest import TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")
MONTH = "2026-12"


def book(user, leave_type, start, end=None, **kwargs):
    return services.book_leave(
        user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY, **kwargs
    )


def page(client, **params):
    return client.get(reverse("floors"), {"month": MONTH, **params})


def post_month(client, team, **floors):
    data = {"team": team.pk, "month": MONTH, **{f"floor_{k}": v for k, v in floors.items()}}
    return client.post(reverse("set-month-floors"), data, follow=True)


def post_override(client, team, role, start, end, value):
    return client.post(
        reverse("add-override"),
        {
            "team": team.pk,
            "month": MONTH,
            "role": role.pk,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "min_available": value,
        },
        follow=True,
    )


def floor_on(team, role, d):
    return services.effective_floors(team, role, [d])[d]


def set_month(manager, team, role, value):
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=value)


# ---- access --------------------------------------------------------------------------- #
def test_members_get_403_everywhere(client, techs, team, roles):
    client.force_login(techs[0])
    assert page(client).status_code == 403
    assert client.post(reverse("set-month-floors"), {"team": team.pk}).status_code == 403
    assert client.post(reverse("add-override")).status_code == 403
    assert client.post(reverse("delete-override", args=[1])).status_code == 403


def test_actions_need_post(client, manager):
    client.force_login(manager)
    for name in ("set-month-floors", "add-override"):
        assert client.get(reverse(name)).status_code == 405
    assert client.get(reverse("delete-override", args=[1])).status_code == 405


def test_a_manager_without_teams_sees_an_empty_state(client, other_manager):
    client.force_login(other_manager)
    assert "don't manage any teams" in page(client).content.decode()


# ---- month floor ------------------------------------------------------------------------ #
def test_month_floor_is_set_for_every_role_in_one_step(client, manager, team, roles):
    client.force_login(manager)
    response = post_month(client, team, **{str(roles["Technical"].pk): "2", str(roles["Functional"].pk): "1"})
    assert "Floors for December 2026 saved" in response.content.decode()
    assert floor_on(team, roles["Technical"], day(0)) == 2
    assert floor_on(team, roles["Functional"], day(4)) == 1
    assert AuditLog.objects.filter(action="floor.set_month", actor=manager).count() == 2


def test_blank_boxes_leave_floors_alone_and_bad_values_are_refused(client, manager, team, roles):
    set_month(manager, team, roles["Technical"], 2)
    client.force_login(manager)
    body = post_month(client, team, **{str(roles["Technical"].pk): "", str(roles["Functional"].pk): "-3"}).content.decode()
    assert "enter a whole number" in body
    assert floor_on(team, roles["Technical"], day(0)) == 2
    assert floor_on(team, roles["Functional"], day(0)) is None
    assert "Nothing to save" in post_month(client, team).content.decode()
    assert "enter a whole number" in post_month(client, team, **{str(roles["Technical"].pk): "abc"}).content.decode()


def test_month_floor_keeps_date_overrides(client, manager, team, roles):
    services.set_floor_override(
        actor=manager, team=team, resource_role=roles["Technical"], start_date=day(0), end_date=day(1), min_available=3
    )
    client.force_login(manager)
    post_month(client, team, **{str(roles["Technical"].pk): "1"})
    assert floor_on(team, roles["Technical"], day(0)) == 3 and floor_on(team, roles["Technical"], day(2)) == 1


def test_raising_a_floor_warns_but_never_cancels(client, manager, team, roles, techs, vacation):
    book(techs[0], vacation, day(0))
    book(techs[1], vacation, day(0))
    client.force_login(manager)
    body = post_month(client, team, **{str(roles["Technical"].pk): "2"}).content.decode()
    assert "1 day is now below a floor" in body and "Technical (floor 2, 1 available)" in body
    assert "stays in place" in body
    assert services.find_breaches(team, day(0), day(0), actor=manager)  # still booked


def test_a_floor_above_the_team_size_is_flagged(client, manager, team, roles, techs):
    client.force_login(manager)
    body = post_month(client, team, **{str(roles["Technical"].pk): "9"}).content.decode()
    assert "only 3 in the team" in body


def test_cannot_set_floors_for_another_managers_team(client, other_manager, team, roles):
    mine = Team.objects.create(name="Mine", manager=other_manager, region=Region.objects.create(name="M"))
    client.force_login(other_manager)
    post_month(client, team, **{str(roles["Technical"].pk): "2"})  # not theirs: falls back to their own team
    assert not Floor.objects.filter(team=team).exists()
    assert not Floor.objects.filter(team=mine).exists()  # and the Toronto role id means nothing there


# ---- overrides -------------------------------------------------------------------------- #
def test_override_a_range_then_remove_it(client, manager, team, roles):
    set_month(manager, team, roles["Technical"], 1)
    client.force_login(manager)
    body = post_override(client, team, roles["Technical"], day(1), day(2), 2).content.decode()
    assert "floor set to 2" in body and "Tue 8 Dec 2026 to Wed 9 Dec 2026" in body
    assert floor_on(team, roles["Technical"], day(1)) == 2 and floor_on(team, roles["Technical"], day(3)) == 1
    assert "Remove" in body
    override = Floor.objects.get(kind=Floor.Kind.OVERRIDE)
    removed = client.post(reverse("delete-override", args=[override.pk]), follow=True).content.decode()
    assert "Removed the Technical override" in removed
    assert floor_on(team, roles["Technical"], day(1)) == 1


def test_bad_override_input_is_explained(client, manager, team, roles):
    client.force_login(manager)
    backwards = post_override(client, team, roles["Technical"], day(3), day(1), 2).content.decode()
    assert "cannot be before the start date" in backwards
    negative = post_override(client, team, roles["Technical"], day(1), day(1), -1).content.decode()
    assert "greater than or equal to 0" in negative
    assert not Floor.objects.exists()


def test_override_cannot_use_another_teams_role(client, manager, team, roles):
    other = Team.objects.create(name="Other", manager=manager, region=Region.objects.create(name="O"))
    foreign_role = other.resource_roles.first()
    client.force_login(manager)
    body = post_override(client, team, foreign_role, day(1), day(1), 2).content.decode()
    assert "Select a valid choice" in body and not Floor.objects.exists()


def test_override_that_causes_breaches_warns(client, manager, team, roles, techs, vacation):
    book(techs[0], vacation, day(1))
    client.force_login(manager)
    body = post_override(client, team, roles["Technical"], day(1), day(1), 3).content.decode()
    assert "now below a floor" in body


def test_other_managers_overrides_are_404(client, other_manager, manager, team, roles):
    override = services.set_floor_override(
        actor=manager, team=team, resource_role=roles["Technical"], start_date=day(0), end_date=day(0), min_available=1
    )
    client.force_login(other_manager)
    assert client.post(reverse("delete-override", args=[override.pk])).status_code == 404
    assert Floor.objects.filter(pk=override.pk).exists()


def test_month_floors_cannot_be_deleted_through_the_override_route(client, manager, team, roles):
    set_month(manager, team, roles["Technical"], 1)
    month = Floor.objects.get(kind=Floor.Kind.MONTH)
    client.force_login(manager)
    assert client.post(reverse("delete-override", args=[month.pk])).status_code == 404


# ---- the grid -------------------------------------------------------------------------- #
def test_grid_shows_floor_cover_and_states(client, manager, team, roles, techs, vacation):
    set_month(manager, team, roles["Technical"], 2)
    book(techs[0], vacation, day(0))  # exactly at the floor: no slot left
    services.set_floor_override(
        actor=manager, team=team, resource_role=roles["Technical"], start_date=day(1), end_date=day(1), min_available=3
    )
    client.force_login(manager)
    body = page(client).content.decode()
    assert "December 2026" in body and "Floor 3" in body and "Floor 2" in body
    assert 'class="low"' in body  # day(0): floor 2, 2 available
    assert 'class="ok"' in body
    assert "Date override" in body  # the * marker on the overridden day
    assert 'class="nofloor"' in body  # Functional has no floor

    book(techs[1], vacation, day(2))
    set_month(manager, team, roles["Technical"], 3)  # now 1 off against a floor of 3 on day(0)
    assert 'class="breach"' in page(client).content.decode()


def test_month_navigation_and_bad_month_param(client, manager, team, roles):
    client.force_login(manager)
    body = page(client).content.decode()
    assert "month=2026-11" in body and "month=2027-01" in body
    fallback = client.get(reverse("floors"), {"month": "garbage", "team": "abc"})
    assert fallback.status_code == 200 and "November 2026" in fallback.content.decode()  # frozen today


def test_grid_shows_counts_never_names(client, manager, team, roles, techs, vacation):
    book(techs[0], vacation, day(0))
    client.force_login(manager)
    assert "Tech1" not in page(client).content.decode()
