"""Two people racing for the last slot must not both get it.

Needs PostgreSQL: the guarantee comes from a row lock on the ResourceRole, which
SQLite does not provide. The test is skipped on SQLite; CI runs it on PostgreSQL.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, connections

from accounts.models import User
from leave import services
from leave.exceptions import FloorViolationError
from leave.models import LeaveDay, LeaveType

from .conftest import TODAY, day

pytestmark = pytest.mark.django_db(transaction=True)

ROUNDS = 5


@pytest.fixture(autouse=True)
def _require_postgres():
    if connection.vendor != "postgresql":
        pytest.skip("concurrency test needs PostgreSQL row locks")


def _attempt(barrier, user_id, leave_type_id, date):
    try:
        user = User.objects.get(pk=user_id)
        leave_type = LeaveType.objects.get(pk=leave_type_id)
        barrier.wait(timeout=15)  # release all threads at the same moment
        services.book_leave(user=user, leave_type=leave_type, start_date=date, end_date=date, today=TODAY)
        return "booked"
    except FloorViolationError:
        return "blocked"
    finally:
        connections.close_all()


def test_only_one_of_three_simultaneous_bookings_gets_the_last_slot(team, roles, manager, techs, vacation):
    # Three technicians, floor of 2: exactly one of them may be off on any day.
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=2)

    for offset in range(ROUNDS):
        date = day(offset)  # Monday to Friday, one race per day
        barrier = threading.Barrier(len(techs))
        with ThreadPoolExecutor(max_workers=len(techs)) as pool:
            futures = [pool.submit(_attempt, barrier, t.pk, vacation.pk, date) for t in techs]
            results = sorted(f.result() for f in futures)
        assert results == ["blocked", "blocked", "booked"], f"race on {date}: {results}"
        assert LeaveDay.objects.filter(date=date).count() == 1


def test_simultaneous_bookings_for_different_roles_do_not_block_each_other(team, roles, manager, techs, make_member, vacation):
    functional = [make_member("func1", role="Functional"), make_member("func2", role="Functional"), make_member("func3", role="Functional")]
    for role in ("Technical", "Functional"):
        services.set_month_floor(actor=manager, team=team, resource_role=roles[role], year=2026, month=12, min_available=2)
    barrier = threading.Barrier(2)
    people = [techs[0], functional[0]]  # different roles, same day, both fit under their floors
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_attempt, barrier, p.pk, vacation.pk, day(0)) for p in people]
        assert [f.result() for f in futures] == ["booked", "booked"]
