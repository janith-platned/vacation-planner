import pytest
from django.core.exceptions import ValidationError

from accounts.models import Region, SystemRole, Team, User


@pytest.fixture
def manager(db):
    return User.objects.create_user("mgr", password="correct-horse-battery", system_role=SystemRole.MANAGER)


@pytest.fixture
def region(db):
    return Region.objects.create(name="Ontario")


def test_new_team_gets_default_roles_and_a_vacation_leave_type(manager, region):
    team = Team.objects.create(name="Toronto", manager=manager, region=region)
    assert set(team.resource_roles.values_list("name", flat=True)) == {"Functional", "Technical", "Systems"}
    assert list(team.leave_types.values_list("name", flat=True)) == ["Vacation"]


def test_a_manager_can_manage_several_teams_one_per_region(manager, region):
    other_region = Region.objects.create(name="Quebec")
    Team.objects.create(name="Toronto", manager=manager, region=region)
    Team.objects.create(name="Montreal", manager=manager, region=other_region)
    assert manager.managed_teams.count() == 2


def test_team_manager_must_have_a_manager_or_admin_role(region):
    member = User.objects.create_user("m", password="correct-horse-battery")
    team = Team(name="Bad", manager=member, region=region)
    with pytest.raises(ValidationError):
        team.full_clean()


@pytest.mark.parametrize("days", [[], [7], [0, 0], ["Mon"]])
def test_working_days_are_validated(manager, region, days):
    team = Team(name="T", manager=manager, region=region, working_days=days)
    with pytest.raises(ValidationError):
        team.full_clean()


def test_unknown_time_zone_is_rejected(manager, region):
    with pytest.raises(ValidationError):
        Team(name="T", manager=manager, region=region, timezone="Mars/Olympus").full_clean()


def test_members_need_a_team_and_a_role_from_that_team(manager, region):
    team = Team.objects.create(name="Toronto", manager=manager, region=region)
    other = Team.objects.create(name="Montreal", manager=manager, region=Region.objects.create(name="Quebec"))
    with pytest.raises(ValidationError):
        User(username="x", system_role=SystemRole.MEMBER).full_clean(exclude=["password"])
    wrong_role = other.resource_roles.first()
    with pytest.raises(ValidationError):
        User(username="y", system_role=SystemRole.MEMBER, team=team, resource_role=wrong_role).full_clean(
            exclude=["password"]
        )


def test_production_settings_hash_passwords_with_argon2():
    # The test settings swap in a fast hasher, so check the real settings module.
    from config import settings as production

    assert production.PASSWORD_HASHERS[0].endswith("Argon2PasswordHasher")
