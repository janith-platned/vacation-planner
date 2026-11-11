from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


class ForcePasswordChangeMiddleware:
    """Send users who must change their password to the change-password page first."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and getattr(user, "must_change_password", False):
            allowed = {reverse("password-change"), reverse("logout")}
            static_prefix = "/" + settings.STATIC_URL.strip("/") + "/"
            if request.path not in allowed and not request.path.startswith(static_prefix):
                return redirect("password-change")
        return self.get_response(request)


VIEW_AS_SESSION_KEY = "view_as_manager_id"


class ViewAsMiddleware:
    """Lets an administrator see the manager screens exactly as one manager would.

    Only administrators are ever affected: the session value is ignored for anyone else,
    and a manager who has since been deactivated or demoted silently ends the view.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            manager_id = request.session.get(VIEW_AS_SESSION_KEY)
            if manager_id:
                from .models import SystemRole, User

                manager = None
                if user.system_role == SystemRole.ADMIN:
                    manager = User.objects.filter(pk=manager_id, is_active=True, system_role=SystemRole.MANAGER).first()
                if manager is None:
                    request.session.pop(VIEW_AS_SESSION_KEY, None)
                else:
                    user.view_as_manager = manager
        return self.get_response(request)
