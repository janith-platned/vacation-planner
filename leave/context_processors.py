from accounts.models import SystemRole

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
    return {"pending_approvals": count}
