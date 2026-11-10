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
