"""Create a small, realistic demo: two teams, three roles, floors and bookings.

Safe to run twice (it does nothing if the demo manager already exists). Dates are
relative to today, so the demo always looks current. Demo only: do not use it with
real employee data.

All demo users share one password: the DEMO_PASSWORD environment variable, or a
fixed default for local use. Never use the default on a public host.
"""
from __future__ import annotations

import datetime as dt
import os

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Department, Region, SystemRole, Team, User
from leave import services
from leave.exceptions import LeaveError

DEFAULT_PASSWORD = "demo-pass-1234"

# username, first name, last name, resource role, department
TORONTO_PEOPLE = [
    ("ana", "Ana", "Silva", "Technical", "Delivery"),
    ("raj", "Raj", "Patel", "Technical", "Delivery"),
    ("tom", "Tom", "Chen", "Technical", "Delivery"),
    ("lee", "Lee", "Park", "Technical", "Delivery"),
    ("fay", "Fay", "Novak", "Functional", "Delivery"),
    ("gus", "Gus", "Ortiz", "Functional", "Delivery"),
    ("sia", "Sia", "Mensah", "Systems", "Support"),
    ("ian", "Ian", "Walsh", "Systems", "Support"),
]
MONTREAL_PEOPLE = [
    ("zoe", "Zoe", "Tremblay", "Technical", "Delivery"),
    ("max", "Max", "Gagnon", "Technical", "Delivery"),
    ("eli", "Eli", "Roy", "Technical", "Support"),
    ("noa", "Noa", "Bouchard", "Functional", "Delivery"),
]
TORONTO_FLOORS = {"Technical": 2, "Functional": 1, "Systems": 1}
MONTREAL_FLOORS = {"Technical": 2, "Functional": 1, "Systems": 0}


def _add_months(first_of_month: dt.date, months: int) -> dt.date:
    index = first_of_month.year * 12 + first_of_month.month - 1 + months
    return dt.date(index // 12, index % 12 + 1, 1)


class Command(BaseCommand):
    help = "Create demo teams, people, floors and bookings (idempotent)."

    def handle(self, *args, **options):
        if User.objects.filter(username="maya").exists():
            self.stdout.write("Demo data already present; nothing to do.")
            return

        env_password = os.environ.get("DEMO_PASSWORD")
        password = env_password or DEFAULT_PASSWORD
        with transaction.atomic():
            self._create(password)

        self.stdout.write(self.style.SUCCESS("Demo data created."))
        self.stdout.write("Sign in as: admin (administrator), maya (manager of two teams), tom (team member).")
        if env_password:
            self.stdout.write("The password is the DEMO_PASSWORD environment variable.")
        else:
            self.stdout.write(f"Password for every demo user: {DEFAULT_PASSWORD}")

    def _create(self, password: str) -> None:
        delivery = Department.objects.create(name="Delivery")
        support = Department.objects.create(name="Support")
        departments = {"Delivery": delivery, "Support": support}
        ontario = Region.objects.create(name="Ontario")
        quebec = Region.objects.create(name="Quebec")

        def person(username, first, last, role=SystemRole.MEMBER, **extra):
            return User.objects.create_user(
                username,
                password=password,
                first_name=first,
                last_name=last,
                system_role=role,
                must_change_password=False,
                **extra,
            )

        person("admin", "Alex", "Admin", SystemRole.ADMIN)
        maya = person("maya", "Maya", "Manager", SystemRole.MANAGER)

        toronto = Team.objects.create(name="Toronto", manager=maya, region=ontario, timezone="America/Toronto")
        montreal = Team.objects.create(name="Montreal", manager=maya, region=quebec, timezone="America/Toronto")

        for team, people in ((toronto, TORONTO_PEOPLE), (montreal, MONTREAL_PEOPLE)):
            roles = {r.name: r for r in team.resource_roles.all()}
            for username, first, last, role, department in people:
                person(
                    username,
                    first,
                    last,
                    team=team,
                    resource_role=roles[role],
                    department=departments[department],
                )

        today = services.today_for(toronto)
        this_month = today.replace(day=1)
        monday = today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)

        # Floors for this month and the next two, set the way a manager would.
        for team, floors in ((toronto, TORONTO_FLOORS), (montreal, MONTREAL_FLOORS)):
            for role in team.resource_roles.all():
                for offset in range(3):
                    month = _add_months(this_month, offset)
                    services.set_month_floor(
                        actor=maya,
                        team=team,
                        resource_role=role,
                        year=month.year,
                        month=month.month,
                        min_available=floors[role.name],
                    )
        # A "release week" with a higher Technical floor, to show a date override.
        services.set_floor_override(
            actor=maya,
            team=toronto,
            resource_role=toronto.resource_roles.get(name="Technical"),
            start_date=monday + dt.timedelta(days=14),
            end_date=monday + dt.timedelta(days=18),
            min_available=3,
        )

        def book(username, start_offset, end_offset):
            user = User.objects.get(username=username)
            leave_type = user.team.leave_types.get(name="Vacation")
            try:
                services.book_leave(
                    user=user,
                    leave_type=leave_type,
                    start_date=monday + dt.timedelta(days=start_offset),
                    end_date=monday + dt.timedelta(days=end_offset),
                )
            except LeaveError as exc:  # the demo must never fail the build
                self.stderr.write(f"Skipped demo booking for {username}: {exc}")

        book("ana", 0, 2)  # Mon-Wed
        book("raj", 1, 3)  # Tue-Thu: two technicians off Tue and Wed, floor still holds
        book("tom", 7, 11)  # the following week
        book("lee", 14, 25)  # long request: goes to the manager for approval
        book("fay", 2, 4)
        book("gus", 8, 8)
        book("sia", 3, 3)
        book("zoe", 7, 9)
