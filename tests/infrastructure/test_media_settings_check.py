import pytest
from django.core import checks
from pytest_django.fixtures import Settings


def _media_errors() -> list[str]:
    return [
        message.id or ""
        for message in checks.run_checks()
        if message.id == "strata.E002"
    ]


@pytest.mark.parametrize(
    "media_url",
    ["media/", "/media/", "//cdn.example.invalid/media/", "ftp://example.invalid/"],
)
def test_relative_media_url_is_an_error_without_debug(
    settings: Settings, media_url: str
) -> None:
    settings.DEBUG = False
    settings.MEDIA_URL = media_url

    assert _media_errors() == ["strata.E002"]


@pytest.mark.parametrize(
    "media_url",
    ["https://cdn.example.invalid/media/", "http://localhost:8000/media/"],
)
def test_absolute_media_url_passes(settings: Settings, media_url: str) -> None:
    settings.DEBUG = False
    settings.MEDIA_URL = media_url

    assert _media_errors() == []


def test_relative_media_url_is_allowed_in_debug(settings: Settings) -> None:
    settings.DEBUG = True
    settings.MEDIA_URL = "media/"

    assert _media_errors() == []
