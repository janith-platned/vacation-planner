# Vacation Planner (Team Leave Management Platform)

Multi-user web app for booking and approving team leave. A booking is accepted only if the manager-defined daily minimum headcount per resource role still holds afterward. Longer requests go to the manager for approval.

## Status

Specification complete. Built and tested: data model, floor check, booking, approval, cancellation, breach detection, anonymous calendar API, and the first screens (sign-in with forced password change, shared calendar where you book leave by dragging across days, booking form with live floor check, My leave with cancel). Dragging over weekends and holidays skips them automatically; tap-first-day then tap-last-day and keyboard selection also work.

Not built yet: manager screens (approvals, floors grid, breaches, team and settings), notifications, the administrator team filters and view-as, Entra SSO. The screens are plain server-rendered Django templates; HTMX is not used yet.

Sign-in: create the first administrator with `python manage.py createsuperuser`. Accounts created any other way must change their password at first sign-in.

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

## Demo data

```
python manage.py seed_demo
```

Creates two teams (Toronto, Montreal) managed by one manager, 12 members, floors for three months with a date override, and sample bookings including one pending request. Users: `admin` (administrator), `maya` (manager), `tom` and others (members). The password is `DEMO_PASSWORD` if set, otherwise `demo-pass-1234`. Demo only; never use it with real data or on a public host with the default password.

## Deploy a free demo on Render

`render.yaml` defines a free web service and a free PostgreSQL database. Render's free tier is for demos: the service sleeps after 15 minutes idle (first request is slow) and the free database expires after 30 days. Use demo data only; Render is not approved for real employee data.

1. In Render choose **New > Blueprint**, connect this GitHub repo, and apply.
2. Wait for the build. It installs, collects static files, migrates and runs `seed_demo`.
3. Open the service URL. The shared demo password is the generated `DEMO_PASSWORD` value in the service's Environment tab.

Configuration is by environment variable: `DATABASE_URL`, `DJANGO_SECRET_KEY`, `DJANGO_BEHIND_PROXY`, `DEMO_SEED`, `DEMO_PASSWORD` (see `render.yaml`).

## Stack

Python/Django, PostgreSQL, HTMX, Azure hosting, GitHub Actions for CI/CD.

## Documentation

- [Specification](docs/Team%20Leave%20Management%20Platform%20-%20Specification.docx)
