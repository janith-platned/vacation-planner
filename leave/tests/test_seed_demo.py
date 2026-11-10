import pytest
from django.core.management import call_command
from django.urls import reverse

from accounts.models import Team, User
from leave import services
from leave.management.commands.seed_demo import DEFAULT_PASSWORD
from leave.models import AuditLog, Floor, LeaveRequest


@pytest.fixture
def seeded(db, capsys):
    call_command("seed_demo")
    return capsys.readouterr()


def test_creates_two_teams_for_one_manager(seeded):
    maya = User.objects.get(username="maya")
    assert set(maya.managed_teams.values_list("name", flat=True)) == {"Toronto", "Montreal"}
    assert Team.objects.count() == 2
    assert User.objects.get(username="admin").system_role == "admin"
    assert User.objects.filter(system_role="member").count() == 12


def test_creates_floors_overrides_and_bookings(seeded):
    assert Floor.objects.filter(kind=Floor.Kind.OVERRIDE).count() == 1
    assert Floor.objects.filter(kind=Floor.Kind.MONTH).count() == 3 * (3 + 3)  # 3 months, 3 roles in each team
    statuses = set(LeaveRequest.objects.values_list("status", flat=True))
    assert {"approved", "pending"} <= statuses
    assert AuditLog.objects.filter(action="floor.set_month").exists()


def test_seeded_bookings_respect_every_floor(seeded):
    for team in Team.objects.all():
        breaches = services.find_breaches(
            team,
            LeaveRequest.objects.order_by("start_date").first().start_date,
            LeaveRequest.objects.order_by("-end_date").first().end_date,
            actor=team.manager,
        )
        assert breaches == []
    assert "Skipped demo booking" not in seeded.err


def test_running_twice_changes_nothing(db, capsys):
    call_command("seed_demo")
    before = (User.objects.count(), LeaveRequest.objects.count(), Floor.objects.count())
    call_command("seed_demo")
    assert (User.objects.count(), LeaveRequest.objects.count(), Floor.objects.count()) == before
    assert "already present" in capsys.readouterr().out


def test_password_comes_from_the_environment_when_set(db, monkeypatch, capsys):
    monkeypatch.setenv("DEMO_PASSWORD", "a-long-random-demo-secret")
    call_command("seed_demo")
    assert DEFAULT_PASSWORD not in capsys.readouterr().out
    assert User.objects.get(username="tom").check_password("a-long-random-demo-secret")
    assert not User.objects.get(username="tom").check_password(DEFAULT_PASSWORD)


def test_demo_users_can_sign_in_and_see_their_screens(seeded, client):
    for username, url in (("tom", reverse("calendar")), ("maya", reverse("calendar")), ("tom", reverse("my-leave"))):
        assert client.login(username=username, password=DEFAULT_PASSWORD)
        assert client.get(url).status_code == 200
    # the manager gets the team switcher with both teams
    client.login(username="maya", password=DEFAULT_PASSWORD)
    body = client.get(reverse("calendar")).content.decode()
    assert "Toronto" in body and "Montreal" in body
