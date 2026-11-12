from accounts.models import EmailChangeRequest, SystemRole

from . import services
from .models import LeaveRequest


def manager_nav(request):
    """Number of requests waiting for the signed-in manager, for the navigation badge."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or user.system_role == SystemRole.MEMBER:
        return {}
    count = LeaveRequest.objects.filter(
        team__in=services.managed_teams(user), status=LeaveRequest.Status.PENDING
    ).count()
    changes = EmailChangeRequest.objects.filter(status=EmailChangeRequest.Status.PENDING)
    if user.system_role != SystemRole.ADMIN:
        changes = changes.filter(approver=user)
    count += changes.count()
    return {"pending_approvals": count, "view_as_manager": getattr(user, "view_as_manager", None)}
