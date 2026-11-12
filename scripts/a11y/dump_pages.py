"""Render every screen from the demo data to /tmp/pages for the axe scan (see axe_scan.js).

    python manage.py migrate && python manage.py seed_demo
    DJANGO_DEBUG=1 PYTHONPATH=. python scripts/a11y/dump_pages.py
"""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from accounts.models import User  # noqa: E402
from leave.models import LeaveRequest  # noqa: E402

OUT = "/tmp/pages"
os.makedirs(OUT, exist_ok=True)
leave_request = LeaveRequest.objects.first()
member = User.objects.filter(system_role="member").first()
SCREENS = [
    ("tom", "calendar", []), ("tom", "book-leave", []), ("tom", "my-leave", []),
    ("maya", "approvals", []), ("maya", "log-leave", []), ("maya", "floors", []), ("maya", "breaches", []),
    ("maya", "members", []), ("maya", "settings", []), ("maya", "audit", []),
    ("maya", "request-detail", [leave_request.pk]), ("maya", "member-detail", [member.pk]),
    ("admin", "console", []),
]
for username, name, args in SCREENS:
    client = Client(HTTP_HOST="localhost")
    client.force_login(User.objects.get(username=username))
    response = client.get(reverse(name, args=args), follow=True)
    open(f"{OUT}/{username}-{name}.html", "w").write(response.content.decode())
    print(username, name, response.status_code)
open(f"{OUT}/login.html", "w").write(Client(HTTP_HOST="localhost").get(reverse("login")).content.decode())
