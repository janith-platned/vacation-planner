"""Settings used by the test suite."""
import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")

from .settings import *  # noqa: E402,F401,F403

# Fast hasher for tests only.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# No collectstatic step in tests, so skip the manifest storage.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
