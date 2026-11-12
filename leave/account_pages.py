"""The Account page: contact details and password for every signed-in person."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import EmailChangeRequest, SystemRole

from . import account_services as svc
from .exceptions import LeaveError


def _run(request, action, success: str):
    try:
        result = action()
    except LeaveError as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, success)
    return result


@login_required
def account_page(request):
    user = request.user
    approver = svc.email_approver(user)
    return render(
        request,
        "leave/account.html",
        {
            "approver": approver,
            "pending": user.email_changes.filter(status=EmailChangeRequest.Status.PENDING).first(),
            "recent": user.email_changes.exclude(status=EmailChangeRequest.Status.PENDING).select_related("decided_by")[:5],
            "reports_to": user.reports_to if user.system_role == SystemRole.MANAGER else None,
        },
    )


@login_required
@require_POST
def update_phone_view(request):
    _run(request, lambda: svc.update_phone(user=request.user, phone=request.POST.get("phone", "")), "Contact number saved.")
    return redirect("account")


@login_required
@require_POST
def request_email_view(request):
    try:
        _, applied = svc.request_email_change(
            user=request.user, new_email=request.POST.get("email", ""), password=request.POST.get("password", "")
        )
    except LeaveError as exc:
        messages.error(request, str(exc))
    else:
        approver = svc.email_approver(request.user)
        if applied:
            messages.success(request, "Your email address has been changed.")
        else:
            who = approver.get_full_name() or approver.username
            messages.success(request, f"Sent to {who} for approval. Your current email stays in use until it is approved.")
    return redirect("account")


@login_required
@require_POST
def cancel_email_view(request, pk: int):
    change = get_object_or_404(EmailChangeRequest, pk=pk, user=request.user)
    _run(request, lambda: svc.cancel_email_change(user=request.user, change=change), "Email change cancelled.")
    return redirect("account")


@login_required
@require_POST
def decide_email_view(request, pk: int):
    if request.user.system_role == SystemRole.MEMBER:
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    qs = EmailChangeRequest.objects.select_related("user")
    if request.user.system_role != SystemRole.ADMIN:
        qs = qs.filter(approver=request.user)
    change = get_object_or_404(qs, pk=pk)
    approve = request.POST.get("decision") == "approve"
    if request.POST.get("decision") not in ("approve", "reject"):
        messages.error(request, "Choose Approve or Reject.")
        return redirect("approvals")
    name = change.user.get_full_name() or change.user.username
    _run(
        request,
        lambda: svc.decide_email_change(actor=request.user, change=change, approve=approve, comment=request.POST.get("comment", "").strip()),
        f"{'Approved' if approve else 'Rejected'} {name}'s email change.",
    )
    return redirect("approvals")
