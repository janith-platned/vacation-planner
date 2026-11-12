# Vacation Planner: Operations Runbook

Short guide for whoever deploys and looks after the app. It covers what exists today (Render demo, CI, local setup) and marks what is **planned** for production (Azure). Anything marked *planned* has not been built or tested yet.

## 1. What runs where

| Piece | Today (demo) | Planned (production) |
|---|---|---|
| Web app | Render web service, `gunicorn config.wsgi:application` | Azure App Service (or container) |
| Database | Render free PostgreSQL | Azure Database for PostgreSQL |
| Secrets | Render environment variables | Azure Key Vault |
| Email | Console backend (printed to the log) | SMTP or Microsoft Graph |
| Sign-in | Username and password | Microsoft Entra SSO (phase 2) |
| Code and CI | GitHub, `janith-platned/vacation-planner` | Platned GitHub organisation |

The demo is for **demo data only**. Do not put real employee data on it.

## 2. Configuration (environment variables)

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Required unless `DJANGO_DEBUG=1`. Long random value, never committed. |
| `DJANGO_DEBUG` | `1` for local development only. Never in production. |
| `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | Comma-separated hosts and `https://` origins. |
| `DJANGO_BEHIND_PROXY` | `1` only when a TLS proxy always sets `X-Forwarded-Proto`. |
| `DATABASE_URL` | Hosted database. Otherwise `POSTGRES_*` variables, otherwise SQLite (local only). |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | Real email. Without `EMAIL_HOST`, emails are only printed to the log. |
| `DEFAULT_FROM_EMAIL`, `SITE_URL` | Sender address and the link base used in emails. |
| `DEMO_SEED`, `DEMO_PASSWORD` | Demo only: seed demo data on deploy; the password for every demo login. |
| `WEB_CONCURRENCY` | Number of gunicorn workers. |

## 3. Deploy

Normal change: feature branch, pull request, CI green, merge to `main`. The deploy then runs `build.sh`:

1. `pip install -r requirements.txt`
2. `collectstatic`
3. `migrate` (migrations run automatically on every deploy)
4. `seed_demo` only if `DEMO_SEED=1`

CI runs `makemigrations --check` and the full test suite (including the PostgreSQL concurrency test). A model change without a migration fails CI. Never merge a red build.

**First administrator:** `python manage.py createsuperuser` (it becomes an Administrator). Everyone else is created in the app and must change their password at first sign-in.

## 4. Rollback

- **Code:** revert the merge commit on `main` through a pull request. The deploy repeats.
- **Migrations:** a revert does not undo applied migrations. For a schema change, write a new migration that reverses it, or restore the database from backup (section 5). Prefer additive migrations (new columns and tables) so a code rollback stays safe.

## 5. Backup and restore

- **Demo:** Render's free database has no backup guarantee and is not suitable for real data.
- **Production (*planned*):** use managed PostgreSQL automated backups with point-in-time restore, plus a periodic `pg_dump` kept elsewhere.
  - Set and document the retention period and the recovery targets (how much data loss and downtime are acceptable).
  - **Test a restore** into a separate database before go-live and then on a schedule. An untested backup is not a backup.

Manual dump and restore:

```bash
pg_dump --format=custom --file=vp.dump "$DATABASE_URL"
pg_restore --clean --if-exists --no-owner --dbname="$TARGET_DATABASE_URL" vp.dump
```

## 6. Secrets

- Never commit secrets, put them in remote URLs, or paste them into chats or tickets.
- GitHub personal access tokens used for pushing must be short-lived and revoked when no longer needed. Revoke any token that was ever shared in a chat.
- **Rotate `DJANGO_SECRET_KEY`:** set the new value and redeploy. Everyone is signed out (sessions are invalidated). Do this if it may have leaked.
- **Rotate the database password:** change it in the database service, update `DATABASE_URL`, redeploy.
- In production, secrets live in Key Vault (*planned*), not in app settings screens or files.

## 7. Email

- Notifications are written to the `Notification` table inside the same transaction as the change, and sent after commit. If sending fails, the failure is recorded.
- Retry failed or pending emails: `python manage.py send_notifications` (up to 5 attempts each). Run it on a schedule in production (every few minutes).
- For real delivery, set the `EMAIL_*` variables and make sure the sending domain passes SPF and DKIM, or messages will land in spam.

## 8. Common tasks

| Task | How |
|---|---|
| Locked-out user | A manager resets the password on the Team screen (shows a one-time temporary password). An administrator can reset a manager from the Administrator Console. |
| Someone left | Deactivate them on the Team screen. Leave that has not started is cancelled. |
| Change who approves a manager's email | Administrator Console, Managers, "Reports to". |
| See who changed what | Audit Log screen, with CSV export. |
| Check a page as a manager | Administrator Console, "View as manager". |

## 9. Monitoring (*planned*)

Nothing watches the app today. Before real data:

- Send application logs to Azure Monitor / Application Insights.
- Alerts: HTTP 5xx rate, failed deploys, database storage and connections, failed notification emails (rows with `sent_at` empty and `attempts` at the maximum).
- A simple uptime check on the sign-in page.

## 10. Security checklist before go-live

- [ ] Login rate limiting or lockout in place
- [ ] HTTPS only, `DJANGO_DEBUG` unset, `DJANGO_BEHIND_PROXY=1` set correctly
- [ ] Secrets in Key Vault; no shared or old tokens left active
- [ ] Backups configured and a restore tested
- [ ] Monitoring and alerts live
- [ ] Email domain verified (SPF and DKIM)
- [ ] Branch protection enforced on `main` in the Platned organisation
- [ ] Data retention rule agreed for leave history and the audit log
- [ ] IT security review completed
- [ ] Keyboard and screen-reader accessibility pass done

## 11. Key-person risk

The app is Python/Django, which is a different stack from IFS. Keep at least one more person able to deploy, restore and fix a failing build. This runbook is the starting point: have that person do a deploy and a restore from it, then fix whatever was unclear.
