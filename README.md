# Vacation Planner (Team Leave Management Platform)

Multi-user web app for booking and approving team leave. A booking is accepted only if the manager-defined daily minimum headcount per resource role still holds afterward. Longer requests go to the manager for approval.

## Status

Specification complete. Implementation not started.

## Key rules

- Managers set a floor per resource role for a whole month, then override specific dates or ranges.
- Team members see anonymous counts and the floor for their own role only.
- Roles: Team member, Manager (manages one or more teams), Administrator (all teams, view-as any manager).
- Login: username/password first, Microsoft Entra SSO later.

## Planned stack

Python/Django, PostgreSQL, HTMX, Azure hosting, GitHub Actions for CI/CD.

## Documentation

- [Specification](docs/Team%20Leave%20Management%20Platform%20-%20Specification.docx)
