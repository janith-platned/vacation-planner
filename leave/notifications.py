"""Email notifications.

Rules of thumb:
- Managers hear about requests that need their decision.
- Employees hear about anything a manager did to their leave.
- Nobody is emailed about their own action, and nobody is emailed anything about colleagues:
  an employee's message contains only their own leave.
- Rows are written inside the caller's transaction; the email is sent after it commits.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import LeaveRequest, Notification

log = logging.getLogger(__name__)
Status = LeaveRequest.Status
MAX_ATTEMPTS = 5


def _day(d) -> str:
    return f"{d:%a} {d.day} {d:%b %Y}"


def _dates(r: LeaveRequest) -> str:
    return _day(r.start_date) if r.start_date == r.end_date else f"{_day(r.start_date)} to {_day(r.end_date)}"


def _name(user) -> str:
    return user.get_full_name() or user.username


def _link(name: str) -> str:
    return settings.SITE_URL.rstrip("/") + reverse(name)


def _queue(*, event: str, recipient, subject: str, body: str, request: LeaveRequest) -> None:
    if not recipient.is_active or not recipient.email:
        return  # nobody to write to; the screens still show the change
    note = Notification.objects.create(
        recipient=recipient, event=event, subject=f"[Vacation Planner] {subject}"[:200], body=body, request=request
    )
    transaction.on_commit(lambda: deliver(note.pk))


def deliver(pk: int) -> bool:
    """Send one queued notification. Records the outcome and never raises."""
    note = Notification.objects.select_related("recipient").filter(pk=pk, sent_at__isnull=True).first()
    if note is None or note.attempts >= MAX_ATTEMPTS:
        return False
    note.attempts += 1
    try:
        send_mail(note.subject, note.body, settings.DEFAULT_FROM_EMAIL, [note.recipient.email], fail_silently=False)
    except Exception as exc:  # delivery problems must not break leave handling
        log.warning("Notification %s failed: %s", pk, exc)
        note.error = str(exc)[:500]
        note.save(update_fields=["attempts", "error"])
        return False
    note.sent_at = timezone.now()
    note.error = ""
    note.save(update_fields=["attempts", "sent_at", "error"])
    return True


def send_pending() -> tuple[int, int]:
    """Retry everything unsent. Returns (sent, still_failing)."""
    sent = failed = 0
    for pk in Notification.objects.filter(sent_at__isnull=True, attempts__lt=MAX_ATTEMPTS).values_list("pk", flat=True):
        if deliver(pk):
            sent += 1
        else:
            failed += 1
    return sent, failed


# ---- who gets told what ------------------------------------------------------------------------ #
def _to_manager(r: LeaveRequest, actor, event: str, headline: str) -> None:
    team = r.team
    manager = team.manager
    if not team.notify_manager_new_request or manager.pk == actor.pk:
        return
    lines = [
        headline,
        "",
        f"Employee: {_name(r.user)}",
        f"Dates: {_dates(r)} ({r.working_days} working day{'s' if r.working_days != 1 else ''})",
        f"Type: {r.leave_type.name}",
        "",
        f"Review it: {_link('approvals')}",
    ]
    _queue(event=event, recipient=manager, subject=f"{headline} {_name(r.user)}, {_dates(r)}", body="\n".join(lines), request=r)


def _to_employee(r: LeaveRequest, actor, event: str, headline: str, note: str = "") -> None:
    if not r.team.notify_employee_updates or r.user_id == actor.pk:
        return
    lines = [f"{headline}", "", f"Dates: {_dates(r)} ({r.working_days} working day{'s' if r.working_days != 1 else ''})", f"Type: {r.leave_type.name}"]
    if note:
        lines += ["", f"Note from {_name(actor)}: {note}"]
    lines += ["", f"Your leave: {_link('my-leave')}"]
    _queue(event=event, recipient=r.user, subject=headline, body="\n".join(lines), request=r)


def booked(r: LeaveRequest, actor) -> None:
    if r.status == Status.PENDING:
        _to_manager(r, actor, "request.pending", "New leave request waiting for approval:")
    elif r.user_id != actor.pk:
        _to_employee(r, actor, "leave.logged", f"{_name(actor)} logged leave for you (approved).", r.decision_comment)


def modified(r: LeaveRequest, actor) -> None:
    if r.status == Status.PENDING:
        _to_manager(r, actor, "request.pending", "Changed leave request waiting for approval:")
    elif r.user_id != actor.pk:
        _to_employee(r, actor, "leave.changed", f"{_name(actor)} changed your leave.", r.decision_comment)


def decided(r: LeaveRequest, actor, approved: bool) -> None:
    verb = "approved" if approved else "rejected"
    _to_employee(r, actor, f"request.{verb}", f"Your leave request was {verb}.", r.decision_comment)


def revoked(r: LeaveRequest, actor) -> None:
    _to_employee(r, actor, "leave.revoked", "Your approved leave was rejected.", r.decision_comment)


def cancelled_by_manager(r: LeaveRequest, actor) -> None:
    _to_employee(r, actor, "leave.cancelled", "Your leave was cancelled.", r.cancel_reason)
