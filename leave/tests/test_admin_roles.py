"""Administrators can promote managers to administrator and back, with guard rails."""
import pytest
from django.urls import reverse

from accounts.models import SystemRole, User
from leave import console_services as svc
from leave.exceptions import InvalidRequest, NotAllowed
from leave.models import AuditLog

from .conftest import PASSWORD

pytestmark = pytest.mark.usefixtures("frozen_today")


def post(client, person, role):
    return client.post(reverse("console-set-role", args=[person.pk]), {"role": role}, follow=True).content.decode()


def test_admin_promotes_a_manager_and_it_is_audited(client, admin_user, manager, team):
    client.force_login(admin_user)
    body = post(client, manager, "admin")
    manager.refresh_from_db()
    assert manager.system_role == SystemRole.ADMIN and "is now Administrator" in body
    log = AuditLog.objects.get(action="user.role")
    assert log.actor == admin_user and log.before == {"system_role": "manager"} and log.after == {"system_role": "admin"}
    # the new administrator can open the console and sees every team
    client.force_login(manager)
    assert client.get(reverse("console")).status_code == 200


def test_promotion_clears_the_reporting_line(admin_user, manager, other_manager):
    manager.reports_to = other_manager
    manager.save()
    svc.set_system_role(actor=admin_user, person=manager, role=SystemRole.ADMIN)
    manager.refresh_from_db()
    assert manager.reports_to is None


def test_demote_an_administrator_when_another_remains(client, admin_user, manager):
    svc.set_system_role(actor=admin_user, person=manager, role=SystemRole.ADMIN)
    client.force_login(admin_user)
    assert "is now Manager" in post(client, manager, "manager")
    manager.refresh_from_db()
    assert manager.system_role == SystemRole.MANAGER
    client.force_login(manager)
    assert client.get(reverse("console")).status_code == 403


def test_the_last_active_administrator_cannot_be_demoted(admin_user, manager):
    svc.set_system_role(actor=admin_user, person=manager, role=SystemRole.ADMIN)
    # Defence in depth: even if the acting admin's account has just been deactivated,
    # the only remaining active administrator stays an administrator.
    User.objects.filter(pk=admin_user.pk).update(is_active=False)
    with pytest.raises(InvalidRequest, match="at least one active administrator"):
        svc.set_system_role(actor=admin_user, person=manager, role=SystemRole.MANAGER)
    manager.refresh_from_db()
    assert manager.system_role == SystemRole.ADMIN


def test_nobody_changes_their_own_role(client, admin_user, manager):
    svc.set_system_role(actor=admin_user, person=manager, role=SystemRole.ADMIN)  # a second admin exists
    client.force_login(admin_user)
    assert "You cannot change your own role" in post(client, admin_user, "manager")
    admin_user.refresh_from_db()
    assert admin_user.system_role == SystemRole.ADMIN


def test_only_administrators_can_change_roles(client, manager, other_manager, techs):
    client.force_login(manager)
    assert client.post(reverse("console-set-role", args=[other_manager.pk]), {"role": "admin"}).status_code == 403
    client.force_login(techs[0])
    assert client.post(reverse("console-set-role", args=[other_manager.pk]), {"role": "admin"}).status_code == 403
    with pytest.raises(NotAllowed):
        svc.set_system_role(actor=manager, person=other_manager, role=SystemRole.ADMIN)
    other_manager.refresh_from_db()
    assert other_manager.system_role == SystemRole.MANAGER


def test_members_and_bad_roles_are_refused(client, admin_user, techs, manager):
    client.force_login(admin_user)
    assert client.post(reverse("console-set-role", args=[techs[0].pk]), {"role": "admin"}).status_code == 404
    assert "Choose Manager or Administrator" in post(client, manager, "member")
    manager.refresh_from_db()
    assert manager.system_role == SystemRole.MANAGER


def test_inactive_accounts_cannot_be_changed(admin_user, manager):
    manager.is_active = False
    manager.save()
    with pytest.raises(InvalidRequest, match="Reactivate"):
        svc.set_system_role(actor=admin_user, person=manager, role=SystemRole.ADMIN)


def test_console_lists_administrators_and_offers_the_buttons(client, admin_user, manager):
    client.force_login(admin_user)
    body = client.get(reverse("console")).content.decode()
    assert "Administrators" in body and "Make Administrator" in body
    assert "Make Manager" not in body  # the only administrator is you
    svc.set_system_role(actor=admin_user, person=manager, role=SystemRole.ADMIN)
    body = client.get(reverse("console")).content.decode()
    assert "Make Manager" in body
