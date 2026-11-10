"""Turn a DATABASE_URL (as provided by Render and most hosts) into a Django DATABASES entry."""
from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlparse


def database_from_url(url: str, conn_max_age: int = 600) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"Unsupported database URL scheme: {parsed.scheme!r}")
    config = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "CONN_MAX_AGE": conn_max_age,
    }
    options = dict(parse_qsl(parsed.query))
    if options:
        config["OPTIONS"] = options  # for example sslmode=require
    return config
