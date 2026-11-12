"""Account changes made by the signed-in person: phone, email (with approval), and who approves."""
from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import EmailChangeRequest, SystemRole, User

from . import notifications
from .exceptions import InvalidRequest, InvalidState, NotAllowed
from .services import _audit

Status = EmailChangeRequest.Status
_PHONE = re.compile(r"^\+?[0-9 ()\-.]{6,30}$")


def email_approver(user: User) -> User | None:
    """Who must approve this person's email change. None = they may change it themselves."""
    if user.system_role == SystemRole.MEMBER:
        manager = user.team.manager if user.team_id else None
    elif user.system_role == SystemRole.MANAGER:
        manager = user.reports_to
    else:
        manager = None
    if manager is None or not manager.is_active or manager.pk == user.pk:
        return None
    return manager


def update_phone(*, user: User, phone: str) -> User:
    phone = phone.strip()
    if phone and not _PHONE.match(phone):
        raise InvalidRequest("Enter a phone number using digits, spaces, + ( ) - or . (6 to 30 characters).")
    before = user.phone
    if phone == before:
        return user
    with transaction.atomic():
        user.phone = phone
        user.save(update_fields=["phone"])
        _audit(actor=user, action="account.phone", entity=user, before={"phone": before}, after={"phone": phone})
    return user


def _clean_email(user: User, raw: str) -> str:
    email = raw.strip()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise InvalidRequest("Enter a valid email address.") from exc
    if email.lower() == (user.email or "").lower():
        raise InvalidRequest("That is already your email address.")
    if User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
        raise InvalidRequest("Another account already uses that email address.")
    return email


def request_email_change(*, user: User, new_email: str, password: str) -> tuple[EmailChangeRequest, bool]:
    """Returns (record, applied). Applied at once when nobody needs to approve it."""
    if not user.check_password(password):
        raise InvalidRequest("Your current password is not correct.")
    email = _clean_email(user, new_email)
    approver = email_approver(user)
    try:
        with transaction.atomic():
            if user.email_changes.filter(status=Status.PENDING).exists():
                raise InvalidState("You already have an email change waiting for approval. Cancel it first.")
            change = EmailChangeRequest.objects.create(user=user, old_email=user.email, new_email=email, approver=approver)
            if approver is None:
                change.status, change.decided_by, change.decided_at = Status.APPROVED, user, timezone.now()
                change.save(update_fields=["status", "decided_by", "decided_at"])
                user.email = email
                user.save(update_fields=["email"])
                _audit(actor=user, action="account.email", entity=user, before={"email": change.old_email}, after={"email": email})
            else:
                _audit(actor=user, action="account.email_request", entity=user, after={"new_email": email, "approver": approver.pk})
                notifications.email_change_requested(change)
    except IntegrityError as exc:
        raise InvalidState("You already have an email change waiting for approval. Cancel it first.") from exc
    return change, approver is None


def cancel_email_change(*, user: User, change: EmailChangeRequest) -> None:
    if change.user_id != user.pk:
        raise NotAllowed("This is not your request.")
    if change.status != Status.PENDING:
        raise InvalidState("That request has already been decided.")
    with transaction.atomic():
        change.status = Status.CANCELLED
        change.save(update_fields=["status"])
        _audit(actor=user, action="account.email_cancel", entity=user, after={"new_email": change.new_email})


def decide_email_change(*, actor: User, change: EmailChangeRequest, approve: bool, comment: str = "") -> None:
    if actor.system_role == SystemRole.MEMBER or (
        actor.system_role == SystemRole.MANAGER and change.approver_id != actor.pk
    ):
        raise NotAllowed("Only the person's reporting manager or an administrator can decide this.")
    with transaction.atomic():
        change = EmailChangeRequest.objects.select_for_update().select_related("user").get(pk=change.pk)
        if change.status != Status.PENDING:
            raise InvalidState("That request has already been decided.")
        person = change.user
        if approve:
            if User.objects.filter(email__iexact=change.new_email).exclude(pk=person.pk).exists():
                raise InvalidRequest("Another account now uses that email address, so this cannot be approved.")
            person.email = change.new_email
            person.save(update_fields=["email"])
        change.status = Status.APPROVED if approve else Status.REJECTED
        change.decided_by, change.decided_at, change.comment = actor, timezone.now(), comment[:500]
        change.save(update_fields=["status", "decided_by", "decided_at", "comment"])
        _audit(
            actor=actor, action="account.email_approve" if approve else "account.email_reject", entity=person,
            before={"email": change.old_email}, after={"email": change.new_email},
        )
        notifications.email_change_decided(change)
