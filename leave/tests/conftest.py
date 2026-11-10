import datetime as dt

import pytest

from accounts.models import Region, SystemRole, Team, User

# Fixed dates keep the tests deterministic. TODAY is a Monday; WEEK_START is the
# Monday of a later week, so day(0)..day(4) are Mon-Fri, day(5)/day(6) the weekend.
TODAY = dt.date(2026, 11, 2)
WEEK_START = dt.date(2026, 12, 7)
PASSWORD = "correct-horse-battery"


def day(n: int) -> dt.date:
    return WEEK_START + dt.timedelta(days=n)


@pytest.fixture
def manager(db):
    return User.objects.create_user(
        "manager", password=PASSWORD, system_role=SystemRole.MANAGER, first_name="Mona", last_name="Manager"
    )


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        "admin", password=PASSWORD, system_role=SystemRole.ADMIN, first_name="Ada", last_name="Admin"
    )


@pytest.fixture
def team(db, manager):
    region = Region.objects.create(name="Ontario")
    return Team.objects.create(name="Toronto", manager=manager, region=region)


@pytest.fixture
def roles(team):
    return {r.name: r for r in team.resource_roles.all()}


@pytest.fixture
def vacation(team):
    return team.leave_types.get(name="Vacation")


@pytest.fixture
def make_member(team, roles):
    def _make(username, role="Technical", team_obj=None, role_obj=None):
        team_obj = team_obj or team
        return User.objects.create_user(
            username,
            password=PASSWORD,
            system_role=SystemRole.MEMBER,
            first_name=username.capitalize(),
            last_name="Tester",
            team=team_obj,
            resource_role=role_obj or roles[role],
        )

    return _make


@pytest.fixture
def techs(make_member):
    """Three Technical members, so a floor of 2 leaves exactly one slot per day."""
    return [make_member("tech1"), make_member("tech2"), make_member("tech3")]


@pytest.fixture
def other_manager(db):
    return User.objects.create_user("manager2", password=PASSWORD, system_role=SystemRole.MANAGER)
