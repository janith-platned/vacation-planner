"""UAT batch 3: account page, email change approval, phone privacy, reporting lines."""
import pytest
from django.urls import reverse

from accounts.models import EmailChangeRequest, Region, SystemRole, Team, User
from leave import account_services as svc, console_services
from leave.exceptions import InvalidRequest, NotAllowed
from leave.models import AuditLog, Notification

from .conftest import PASSWORD

pytestmark = pytest.mark.usefixtures("frozen_today")


def post(client, name, data=None, *args):
    return client.post(reverse(name, args=args), data or {}, follow=True).content.decode()


# ---- who approves ------------------------------------------------------------------------ #
def test_approver_rules(techs, manager, admin_user, other_manager):
    assert svc.email_approver(techs[0]) == manager
    assert svc.email_approver(manager) is None  # no reporting line yet
    manager.reports_to = other_manager
    assert svc.email_approver(manager) == other_manager
    assert svc.email_approver(admin_user) is None
    manager.team = None
    other_manager.is_active = False
    assert svc.email_approver(manager) is None  # inactive boss cannot approve


# ---- account page ------------------------------------------------------------------------ #
def test_every_role_can_open_account_and_nav_links_to_it(client, techs, manager, admin_user):
    for u in (techs[0], manager, admin_user):
        client.force_login(u)
        body = client.get(reverse("account")).content.decode()
        assert "Contact number" in body and "Change password" in body
        assert 'href="/account/"' in body or reverse("account") in body
    assert client.get(reverse("account")).status_code == 200


def test_phone_saved_validated_and_audited(client, techs):
    client.force_login(techs[0])
    assert "Contact number saved" in post(client, "account-phone", {"phone": "+1 (416) 555-0100"})
    techs[0].refresh_from_db()
    assert techs[0].phone == "+1 (416) 555-0100"
    assert "Enter a phone number" in post(client, "account-phone", {"phone": "call me"})
    techs[0].refresh_from_db()
    assert techs[0].phone == "+1 (416) 555-0100"
    assert AuditLog.objects.filter(action="account.phone", actor=techs[0]).exists()
    post(client, "account-phone", {"phone": ""})
    techs[0].refresh_from_db()
    assert techs[0].phone == ""


def test_colleagues_never_see_phone_but_manager_does(client, techs, manager):
    techs[0].phone = "555-0101"
    techs[0].save()
    client.force_login(techs[1])
    for name in ("calendar", "my-leave", "account"):
        assert "555-0101" not in client.get(reverse(name)).content.decode()
    client.force_login(manager)
    assert "555-0101" in client.get(reverse("member-detail", args=[techs[0].pk])).content.decode()


# ---- email change ------------------------------------------------------------------------ #
def test_member_email_needs_manager_approval(client, techs, manager):
    client.force_login(techs[0])
    old = techs[0].email
    body = post(client, "account-email", {"email": "new@example.com", "password": PASSWORD})
    assert "for approval" in body
    techs[0].refresh_from_db()
    assert techs[0].email == old
    change = EmailChangeRequest.objects.get()
    assert change.approver == manager and change.status == "pending"
    assert Notification.objects.filter(recipient=manager, event="email.requested").count() == (1 if manager.email else 0)
    # one pending at a time
    assert "already have an email change" in post(client, "account-email", {"email": "x@example.com", "password": PASSWORD})
    client.force_login(manager)
    assert "(1)" in client.get(reverse("calendar")).content.decode()
    assert "new@example.com" in client.get(reverse("approvals")).content.decode()
    assert "Approved" in post(client, "decide-email-change", {"decision": "approve"}, change.pk)
    techs[0].refresh_from_db()
    assert techs[0].email == "new@example.com"
    assert AuditLog.objects.filter(action="account.email_approve", actor=manager).exists()
    assert Notification.objects.filter(recipient=techs[0], event="email.approved").exists()


def test_reject_keeps_old_email(client, techs, manager):
    old = techs[0].email
    change, applied = svc.request_email_change(user=techs[0], new_email="no@example.com", password=PASSWORD)
    assert not applied
    client.force_login(manager)
    post(client, "decide-email-change", {"decision": "reject", "comment": "Use your work address"}, change.pk)
    techs[0].refresh_from_db()
    change.refresh_from_db()
    assert techs[0].email == old and change.status == "rejected" and change.comment == "Use your work address"


def test_no_approver_applies_immediately(client, admin_user):
    client.force_login(admin_user)
    assert "has been changed" in post(client, "account-email", {"email": "ada@example.com", "password": PASSWORD})
    admin_user.refresh_from_db()
    assert admin_user.email == "ada@example.com"
    assert EmailChangeRequest.objects.get().status == "approved"


def test_manager_email_goes_to_reporting_manager(techs, manager, other_manager):
    manager.reports_to = other_manager
    manager.save()
    change, applied = svc.request_email_change(user=manager, new_email="mona@example.com", password=PASSWORD)
    assert not applied and change.approver == other_manager
    with pytest.raises(NotAllowed):
        svc.decide_email_change(actor=techs[0], change=change, approve=True)
    third = User.objects.create_user("third", password=PASSWORD, system_role=SystemRole.MANAGER)
    with pytest.raises(NotAllowed):
        svc.decide_email_change(actor=third, change=change, approve=True)


def test_other_managers_cannot_decide_via_view(client, techs, manager, other_manager):
    change, _ = svc.request_email_change(user=techs[0], new_email="z@example.com", password=PASSWORD)
    client.force_login(other_manager)
    assert client.post(reverse("decide-email-change", args=[change.pk]), {"decision": "approve"}).status_code == 404
    client.force_login(techs[1])
    assert client.post(reverse("decide-email-change", args=[change.pk]), {"decision": "approve"}).status_code == 403


def test_email_validation(techs, manager):
    with pytest.raises(InvalidRequest, match="password"):
        svc.request_email_change(user=techs[0], new_email="a@example.com", password="wrong")
    with pytest.raises(InvalidRequest, match="valid email"):
        svc.request_email_change(user=techs[0], new_email="nope", password=PASSWORD)
    manager.email = "taken@example.com"
    manager.save()
    with pytest.raises(InvalidRequest, match="already uses"):
        svc.request_email_change(user=techs[0], new_email="TAKEN@example.com", password=PASSWORD)
    assert not EmailChangeRequest.objects.exists()


def test_cancel_own_request_only(client, techs):
    change, _ = svc.request_email_change(user=techs[0], new_email="c@example.com", password=PASSWORD)
    client.force_login(techs[1])
    assert client.post(reverse("account-email-cancel", args=[change.pk])).status_code == 404
    client.force_login(techs[0])
    assert "cancelled" in post(client, "account-email-cancel", {}, change.pk)
    change.refresh_from_db()
    assert change.status == "cancelled"
    # can request again afterwards
    svc.request_email_change(user=techs[0], new_email="d@example.com", password=PASSWORD)


def test_decided_request_cannot_be_decided_twice(manager, techs):
    from leave.exceptions import InvalidState

    change, _ = svc.request_email_change(user=techs[0], new_email="e@example.com", password=PASSWORD)
    svc.decide_email_change(actor=manager, change=change, approve=True)
    with pytest.raises(InvalidState):
        svc.decide_email_change(actor=manager, change=change, approve=False)


# ---- reporting lines (admin) ---------------------------------------------------------------- #
def test_admin_sets_reports_to_and_blocks_loops(client, admin_user, manager, other_manager, techs):
    client.force_login(admin_user)
    assert "Reporting line saved" in post(client, "console-reports-to", {"reports_to": other_manager.pk}, manager.pk)
    manager.refresh_from_db()
    assert manager.reports_to == other_manager
    assert "reporting loop" in post(client, "console-reports-to", {"reports_to": manager.pk}, other_manager.pk)
    other_manager.refresh_from_db()
    assert other_manager.reports_to is None
    assert "reporting loop" in post(client, "console-reports-to", {"reports_to": manager.pk}, manager.pk)
    assert AuditLog.objects.filter(action="manager.reports_to").exists()


def test_reports_to_is_admin_only(client, manager, other_manager):
    client.force_login(manager)
    assert client.post(reverse("console-reports-to", args=[other_manager.pk]), {"reports_to": manager.pk}).status_code == 403
    with pytest.raises(NotAllowed):
        console_services.set_reports_to(actor=manager, manager=other_manager, boss=manager)


def test_add_manager_with_reporting_line_and_console_renders(client, admin_user, manager):
    client.force_login(admin_user)
    post(client, "console-add-manager", {"username": "newboss", "first_name": "New", "reports_to": manager.pk})
    assert User.objects.get(username="newboss").reports_to == manager
    assert "Reports to" in client.get(reverse("console")).content.decode()


def test_admin_can_assign_manager_to_team(client, admin_user, manager, other_manager, team):
    client.force_login(admin_user)
    post(client, "console-update-team", {"name": team.name, "manager": other_manager.pk, "region": team.region_id}, team.pk)
    team.refresh_from_db()
    assert team.manager == other_manager


def test_django_admin_link_is_not_shown(client, admin_user, manager, techs):
    for u in (admin_user, manager, techs[0]):
        client.force_login(u)
        body = client.get(reverse("calendar")).content.decode()
        assert "/admin/" not in body
