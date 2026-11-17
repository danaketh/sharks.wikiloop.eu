"""Django system checks for media delivery settings."""

from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, register


@register()
def check_absolute_media_url(**_kwargs: object) -> list[Error]:
    """Require an absolute ``MEDIA_URL`` outside DEBUG.

    Delivery returns storage URLs to a frontend on another host, so a
    path-only URL would resolve against the wrong origin.
    """
    if settings.DEBUG:
        return []
    parts = urlsplit(str(settings.MEDIA_URL))
    if parts.scheme in {"http", "https"} and parts.netloc:
        return []
    return [
        Error(
            "MEDIA_URL must be an absolute http(s) URL when DEBUG is off.",
            hint="Set DJANGO_MEDIA_URL to the public media origin, "
            "e.g. https://media.example.org/.",
            id="strata.E002",
        )
    ]
