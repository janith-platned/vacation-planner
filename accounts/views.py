from django.contrib import messages
from django.contrib.auth.views import PasswordChangeView
from django.urls import reverse_lazy


class ForcedPasswordChangeView(PasswordChangeView):
    """Password change that also clears the 'must change password' flag."""

    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("calendar")

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user
        if user.must_change_password:
            user.must_change_password = False
            user.save(update_fields=["must_change_password"])
        messages.success(self.request, "Your password has been changed.")
        return response
