"""Email notifications: who is told what, privacy, failure handling, and the settings."""
import pytest
from django.core.management import call_command
from django.urls import reverse

from leave import notifications, services
from leave.models import LeaveRequest, Notification

from .conftest import TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")
Status = LeaveRequest.Status


@pytest.fixture(autouse=True)
def emails(manager, techs):
    manager.email = "mona@example.com"
    manager.save()
    for i, tech in enumerate(techs, start=1):
        tech.email = f"tech{i}@example.com"
        tech.save()


@pytest.fixture
def commit(django_capture_on_commit_callbacks):
    """Run the after-commit sending that a real request would do."""
    return lambda: django_capture_on_commit_callbacks(execute=True)


def book(user, leave_type, start, end=None, **kw):
    return services.book_leave(user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY, **kw)


@pytest.fixture
def approval_needed(team):
    team.approval_threshold = 0
    team.save()


# ---- the manager hears about requests waiting ------------------------------------------------ #
def test_a_pending_request_emails_the_manager(commit, mailoutbox, approval_needed, techs, vacation):
    with commit():
        request = book(techs[0], vacation, day(0), day(1))
    assert request.status == Status.PENDING
    assert len(mailoutbox) == 1
    mail = mailoutbox[0]
    assert mail.to == ["mona@example.com"] and "Tech1 Tester" in mail.subject
    assert "Mon 7 Dec 2026 to Tue 8 Dec 2026" in mail.body and "Vacation" in mail.body and "/manage/approvals/" in mail.body
    note = Notification.objects.get()
    assert note.sent_at is not None and note.attempts == 1 and note.request == request


def test_auto_approved_bookings_email_nobody(commit, mailoutbox, techs, vacation):
    with commit():
        book(techs[0], vacation, day(0))
    assert mailoutbox == []


def test_a_managers_own_pending_request_does_not_email_themselves(commit, mailoutbox, team, manager, roles, approval_needed, vacation):
    manager.resource_role, manager.team = roles["Technical"], team
    manager.save()
    team.manager_counts_toward_headcount = True
    team.save()
    with commit():
        book(manager, vacation, day(0))
    assert mailoutbox == []


def test_owner_edit_that_restarts_approval_emails_the_manager(commit, mailoutbox, team, techs, vacation):
    team.approval_threshold = 1
    team.save()
    request = book(techs[0], vacation, day(0))
    assert request.status == Status.APPROVED and mailoutbox == []
    with commit():
        services.modify_leave(request=request, actor=techs[0], start_date=day(0), end_date=day(2), today=TODAY)
    assert len(mailoutbox) == 1 and "Changed leave request" in mailoutbox[0].body


# ---- employees hear about what managers do ----------------------------------------------------- #
def test_approve_and_reject_email_the_employee_with_the_comment(commit, mailoutbox, manager, approval_needed, techs, vacation):
    first = book(techs[0], vacation, day(0))
    second = book(techs[1], vacation, day(1))
    mailoutbox.clear()
    with commit():
        services.decide_request(request=first, actor=manager, approve=True, comment="Enjoy")
        services.decide_request(request=second, actor=manager, approve=False, comment="Go-live week")
    by_recipient = {m.to[0]: m for m in mailoutbox}
    assert "was approved" in by_recipient["tech1@example.com"].subject and "Enjoy" in by_recipient["tech1@example.com"].body
    assert "was rejected" in by_recipient["tech2@example.com"].subject and "Go-live week" in by_recipient["tech2@example.com"].body
    assert "Note from Mona Manager" in by_recipient["tech2@example.com"].body


def test_logging_changing_revoking_and_cancelling_all_email_the_employee(commit, mailoutbox, manager, techs, vacation):
    with commit():
        logged = book(techs[0], vacation, day(0), day(1), actor=manager, comment="By phone")
    assert "logged leave for you" in mailoutbox[-1].subject and "By phone" in mailoutbox[-1].body
    with commit():
        services.modify_leave(request=logged, actor=manager, start_date=day(2), end_date=day(2), today=TODAY)
    assert "changed your leave" in mailoutbox[-1].subject
    with commit():
        services.revoke_approved_leave(request=logged, actor=manager, reason="Customer need", today=TODAY)
    assert "approved leave was rejected" in mailoutbox[-1].subject and "Customer need" in mailoutbox[-1].body
    other = book(techs[1], vacation, day(3))
    with commit():
        services.cancel_leave(request=other, actor=manager, reason="Floor raised", today=TODAY)
    assert "was cancelled" in mailoutbox[-1].subject and "Floor raised" in mailoutbox[-1].body
    assert [m.to[0] for m in mailoutbox] == ["tech1@example.com"] * 3 + ["tech2@example.com"]


def test_employees_cancelling_their_own_leave_email_nobody(commit, mailoutbox, techs, vacation):
    request = book(techs[0], vacation, day(0))
    with commit():
        services.cancel_leave(request=request, actor=techs[0], today=TODAY)
    assert mailoutbox == []


# ---- privacy ----------------------------------------------------------------------------------------- #
def test_employee_emails_never_mention_colleagues(commit, mailoutbox, manager, approval_needed, techs, vacation):
    book(techs[1], vacation, day(0))  # a colleague is off the same day
    mine = book(techs[0], vacation, day(0))
    mailoutbox.clear()
    with commit():
        services.decide_request(request=mine, actor=manager, approve=True)
    body = mailoutbox[0].subject + mailoutbox[0].body
    assert "Tech2" not in body and "tech2" not in body and "available" not in body.lower()


# ---- failure handling and settings ---------------------------------------------------------------------- #
def test_a_mail_failure_is_recorded_and_never_breaks_the_booking(commit, monkeypatch, approval_needed, techs, vacation):
    def boom(*a, **k):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(notifications, "send_mail", boom)
    with commit():
        request = book(techs[0], vacation, day(0))
    assert request.status == Status.PENDING  # the booking stands
    note = Notification.objects.get()
    assert note.sent_at is None and note.attempts == 1 and "smtp down" in note.error


def test_send_notifications_retries_failures_then_gives_up(commit, monkeypatch, mailoutbox, approval_needed, techs, vacation):
    real = notifications.send_mail
    monkeypatch.setattr(notifications, "send_mail", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    with commit():
        book(techs[0], vacation, day(0))
    monkeypatch.setattr(notifications, "send_mail", real)
    call_command("send_notifications")
    note = Notification.objects.get()
    assert note.sent_at is not None and note.attempts == 2 and len(mailoutbox) == 1
    call_command("send_notifications")
    assert len(mailoutbox) == 1  # already sent, not sent twice

    Notification.objects.update(sent_at=None, attempts=notifications.MAX_ATTEMPTS)
    call_command("send_notifications")
    assert len(mailoutbox) == 1  # gave up


def test_nothing_is_sent_if_the_change_rolls_back(commit, mailoutbox, approval_needed, manager, techs, vacation, roles, team):
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=3)
    from leave.exceptions import FloorViolationError

    with commit():
        with pytest.raises(FloorViolationError):
            book(techs[0], vacation, day(0))
    assert mailoutbox == [] and not Notification.objects.exists()


def test_people_without_an_email_or_inactive_are_skipped(commit, mailoutbox, manager, approval_needed, techs, vacation):
    request = book(techs[0], vacation, day(0))
    techs[0].email = ""
    techs[0].save()
    mailoutbox.clear()
    with commit():
        services.decide_request(request=request, actor=manager, approve=True)
    assert mailoutbox == [] and Notification.objects.filter(recipient=techs[0]).count() == 0


def test_team_toggles_switch_each_kind_off(commit, mailoutbox, team, manager, approval_needed, techs, vacation):
    team.notify_manager_new_request = False
    team.save()
    with commit():
        pending = book(techs[0], vacation, day(0))
    assert mailoutbox == []
    team.notify_employee_updates = False
    team.save()
    with commit():
        services.decide_request(request=pending, actor=manager, approve=True)
    assert mailoutbox == []


def test_toggles_are_saved_from_the_settings_screen(client, manager, team):
    client.force_login(manager)
    base = {
        "team": team.pk, "approval_mode": "threshold", "approval_threshold": "5",
        "working_days": ["0", "1", "2", "3", "4"], "timezone": "America/New_York",
    }
    client.post(reverse("save-rules"), {**base, "notify_manager": "1"})
    team.refresh_from_db()
    assert team.notify_manager_new_request and not team.notify_employee_updates
    body = client.get(reverse("settings")).content.decode()
    assert "Email the manager when a request needs approval" in body
    client.post(reverse("save-rules"), {**base, "notify_manager": "1", "notify_employee": "1"})
    team.refresh_from_db()
    assert team.notify_manager_new_request and team.notify_employee_updates
