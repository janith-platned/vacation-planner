# Vacation Planner (Team Leave Management Platform)

Multi-user web app for booking and approving team leave. A booking is accepted only if the manager-defined daily minimum headcount per resource role still holds afterward. Longer requests go to the manager for approval.

## Status

Specification complete. Built and tested: data model, floor check, booking, approval, cancellation, breach detection, anonymous calendar API, and the first screens (sign-in with forced password change, shared calendar where you book leave by dragging across days, booking form with live floor check, My leave with cancel). Dragging over weekends and holidays skips them automatically; tap-first-day then tap-last-day and keyboard selection also work.

Managers approve or reject pending requests on the Approvals screen, which shows each request's cover against today's floors and a badge with the number waiting. Below the pending cards, a searchable, filterable, paginated table lists every request (filter by name, team, role, status and when); each row opens a detail page with the days, cover, history, and the actions to change the dates of approved leave (checked against the floor, with a floor override that needs a reason) or reject it with a required reason shown to the employee. Managers can also log leave on behalf of an employee: it is approved at once, may be in the past or use restricted leave types, shows a live floor check, and the employee sees who logged it.

The Floors screen sets each role's floor for a whole month in one step, shows the month as a calendar of floor and cover per role, and takes date or range overrides (newest wins). Raising a floor warns which days now fall short but never cancels leave.

The Breaches screen lists days below a floor over the next 30 to 180 days with who is booked off, and lets the manager open a request or cancel it with a reason the employee sees.

The Team screen lets a manager add members (with a one-time temporary password), edit their details and role, reset passwords, and deactivate or reactivate them. Deactivating cancels leave that has not started, cuts leave in progress back to the days already taken, and warns if a floor is now short. Changing someone's role is refused while they have leave from today on.

The Settings screen lets a manager set each team's approval threshold (or no approval), working days, time zone and whether the manager counts toward headcount, add or rename resource roles, define leave types (counts toward floor, employee-bookable, always needs approval, own threshold), and manage holidays. Changes never move leave that is already booked.

The administrator console (admins only) lists every team with filters by manager, region and department, lets an administrator "view as" any manager (the manager screens then show exactly that manager's teams, with a banner and an Exit button), and manages regions, departments, teams (one manager and one region each) and manager accounts. A person's department is set on the Team screen.

Email notifications: managers are emailed when a request needs approval; employees are emailed when a manager approves, rejects, changes, cancels or logs their leave. Nobody is emailed about their own action, and an employee's email contains only their own leave. Each team can switch either kind off in Settings. Notifications are queued with the change and sent after it commits; failures are recorded and retried with `python manage.py send_notifications` (gives up after 5 attempts). By default emails are printed to the server log (fine for the demo); set `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` and `SITE_URL` for real delivery.

The Audit screen shows who did what and when (leave, floors, people, settings, administration) with filters by person, team, type and date, and a CSV export that respects the same filters (cells that start like formulas are neutralised). Managers see only their own teams' history; administrators also see organisation-wide changes; while viewing as a manager an administrator sees that manager's teams only. Entries are stamped with their team when written, older entries are assigned one by a migration, and the log cannot be edited or deleted.

Accessibility: every screen has a skip link, one h1, labelled fields, marked current page, and status shown in words as well as colour. The colour palette is checked against WCAG AA contrast (4.5:1) in both light and dark themes by the test suite, and `scripts/a11y/` renders every screen from the demo data and scans it with axe-core (no violations at the last scan). Automated checks cannot replace a real keyboard and screen-reader pass; do one before go-live.

Mobile: below 720px the navigation collapses into a Menu button; tables stack into labelled cards; the calendar shows compact cells (for example "1/3 off", "1 left") while screen readers still get the full wording. Every list that can grow is paged (my leave, team, breaches, console teams/managers/regions/departments, settings roles/leave types/holidays, a person's upcoming leave, floor overrides, waiting requests). Approved and rejected leave shows who decided and when ("Approved automatically" when it was within the approval limit).

Not built yet: Entra SSO, half-days, MFA, calendar sync. The screens are plain server-rendered Django templates; HTMX is not used yet.

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
