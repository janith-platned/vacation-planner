# Vacation Planner (Team Leave Management Platform)

Multi-user web app for booking and approving team leave. A booking is accepted only if the manager-defined daily minimum headcount per resource role still holds afterward. Longer requests go to the manager for approval.

## Status

Specification complete. The core is built and tested: data model, floor check, booking, approval, cancellation, breach detection and the anonymous calendar API. Screens (HTMX), manager settings UI, notifications, the administrator team filters and view-as are not built yet.

## Key rules

- Managers set a floor per resource role for a whole month, then override specific dates or ranges (newest override wins when they overlap).
- A booking is checked on every working day: available people minus the requester must stay at or above the floor. Pending leave also reserves capacity.
- The check and the insert run in one transaction holding a row lock on the resource role, so two people cannot both take the last slot.
- Raising a floor never cancels bookings. Breached days are reported to the manager, who cancels manually.
- Team members see anonymous counts and the floor for their own role only. Names never leave the server for a member session.
- Roles: Team member, Manager (manages one or more teams), Administrator (all teams, view-as any manager).
- Login: username/password first, Microsoft Entra SSO later.

## Layout

| Path | Purpose |
| --- | --- |
| `accounts/` | Users, teams, regions, departments, resource roles |
| `leave/models.py` | Leave types, holidays, floors, requests, leave days, audit log |
| `leave/services.py` | All business rules. State changes go through here |
| `leave/views.py` | Read-only calendar API returning anonymous counts |
| `leave/tests/` | Rule tests, privacy tests, PostgreSQL concurrency test |
| `docs/` | Specification |

## Local development

Requires Python 3.10+ and PostgreSQL (the concurrency guarantee relies on PostgreSQL row locks).

```
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                   # then export the variables in it
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Tests

```
pytest                                   # needs POSTGRES_* variables, see .env.example
python scripts/run_tests_local_pg.py     # no PostgreSQL installed: uses an embedded one
                                         # (pip install -r requirements-local.txt)
```

Without PostgreSQL the suite falls back to SQLite and skips the two concurrency tests. CI always runs them against PostgreSQL 16.

## Stack

Python/Django, PostgreSQL, HTMX, Azure hosting, GitHub Actions for CI/CD.

## Documentation

- [Specification](docs/Team%20Leave%20Management%20Platform%20-%20Specification.docx)
