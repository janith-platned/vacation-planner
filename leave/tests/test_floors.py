"""Floor definition: one month value, date overrides, precedence."""
import pytest

from leave import services
from leave.exceptions import InvalidRequest, NotAllowed
from leave.models import Floor

from .conftest import day


def floors_for(team, role, *offsets):
    return services.effective_floors(team, role, [day(n) for n in offsets])


def test_month_floor_applies_to_every_day_of_the_month(team, roles, manager):
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=2)
    values = floors_for(team, roles["Technical"], 0, 5, 20)
    assert set(values.values()) == {2}


def test_no_floor_outside_the_month_or_for_other_roles(team, roles, manager):
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=2)
    january = services.effective_floors(team, roles["Technical"], [day(30)])  # 6 Jan 2027
    assert january == {day(30): None}
    assert floors_for(team, roles["Systems"], 0) == {day(0): None}


def test_override_changes_only_its_dates(team, roles, manager):
    role = roles["Technical"]
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=2)
    services.set_floor_override(actor=manager, team=team, resource_role=role, start_date=day(1), end_date=day(2), min_available=1)
    assert floors_for(team, role, 0, 1, 2, 3) == {day(0): 2, day(1): 1, day(2): 1, day(3): 2}


def test_newest_override_wins_when_overrides_overlap(team, roles, manager):
    role = roles["Technical"]
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=2)
    services.set_floor_override(actor=manager, team=team, resource_role=role, start_date=day(1), end_date=day(2), min_available=1)
    services.set_floor_override(actor=manager, team=team, resource_role=role, start_date=day(2), end_date=day(2), min_available=0)
    assert floors_for(team, role, 1, 2) == {day(1): 1, day(2): 0}


def test_deleting_an_override_returns_to_the_month_floor(team, roles, manager):
    role = roles["Technical"]
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=2)
    override = services.set_floor_override(actor=manager, team=team, resource_role=role, start_date=day(1), end_date=day(1), min_available=0)
    services.delete_floor_override(actor=manager, floor=override)
    assert floors_for(team, role, 1) == {day(1): 2}


def test_changing_the_month_floor_keeps_overrides(team, roles, manager):
    role = roles["Technical"]
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=2)
    services.set_floor_override(actor=manager, team=team, resource_role=role, start_date=day(1), end_date=day(1), min_available=0)
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=3)
    assert floors_for(team, role, 0, 1) == {day(0): 3, day(1): 0}
    assert Floor.objects.filter(kind=Floor.Kind.MONTH, resource_role=role).count() == 1


def test_only_the_teams_manager_or_admin_can_set_floors(team, roles, techs, other_manager, admin_user):
    role = roles["Technical"]
    for intruder in (techs[0], other_manager):
        with pytest.raises(NotAllowed):
            services.set_month_floor(actor=intruder, team=team, resource_role=role, year=2026, month=12, min_available=1)
    services.set_month_floor(actor=admin_user, team=team, resource_role=role, year=2026, month=12, min_available=1)


def test_role_must_belong_to_the_team(team, roles, manager, other_manager):
    from accounts.models import Region, Team

    other_team = Team.objects.create(name="Other", manager=other_manager, region=Region.objects.create(name="BC"))
    foreign_role = other_team.resource_roles.get(name="Technical")
    with pytest.raises(InvalidRequest):
        services.set_month_floor(actor=manager, team=team, resource_role=foreign_role, year=2026, month=12, min_available=1)
