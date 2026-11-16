from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from pytest_django.fixtures import Settings
from rest_framework.test import APIClient

from strata_cms.infrastructure.persistence.django.models import MediaBlobRecord

pytestmark = pytest.mark.django_db

URL_NAME = "api:management:media-blob-upload"


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def _client(*codenames: str, username: str = "uploader") -> APIClient:
    user = get_user_model().objects.create_user(
        username=username, password="test-password", is_staff=True
    )
    user.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="strata_persistence", codename__in=codenames
        )
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _uploader() -> APIClient:
    return _client("upload_mediablobrecord")


def _png(size: tuple[int, int] = (6, 4)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, (0, 80, 160)).save(buffer, "PNG")
    return buffer.getvalue()


def _file(
    data: bytes, name: str = "shark.png", content_type: str = "image/png"
) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type=content_type)


def test_upload_returns_201_and_server_generated_key(media_root: Path) -> None:
    response = _uploader().post(
        reverse(URL_NAME),
        {"file": _file(_png(), name="../../evil.php", content_type="text/x-php")},
        format="multipart",
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "blob_id",
        "content_type",
        "size_bytes",
        "width",
        "height",
        "sha256",
    }
    assert body["content_type"] == "image/png"
    assert (body["width"], body["height"]) == (6, 4)
    record = MediaBlobRecord.objects.get(pk=UUID(body["blob_id"]))
    hex_id = UUID(body["blob_id"]).hex
    assert record.storage_key == (
        f"media/{hex_id[0:2]}/{hex_id[2:4]}/{body['blob_id']}.png"
    )
    assert response.wsgi_request.path == "/api/v1/manage/media/blobs/"
    assert record.sha256 == body["sha256"]
    assert record.size_bytes == body["size_bytes"]
    assert "evil" not in record.storage_key
    assert (media_root / record.storage_key).is_file()
    assert not list(media_root.rglob("*.php"))


def test_upload_preserve_original_keeps_bytes(media_root: Path) -> None:
    data = _png()

    response = _uploader().post(
        reverse(URL_NAME),
        {"file": _file(data), "preserve_original": "true"},
        format="multipart",
    )

    assert response.status_code == 201
    record = MediaBlobRecord.objects.get(pk=UUID(response.json()["blob_id"]))
    assert (media_root / record.storage_key).read_bytes() == data


def test_upload_over_limit_returns_400_media_too_large(
    settings: Settings,
) -> None:
    data = _png()
    settings.STRATA_MEDIA_MAX_UPLOAD_BYTES = len(data) - 1

    response = _uploader().post(
        reverse(URL_NAME), {"file": _file(data)}, format="multipart"
    )

    assert response.status_code == 400
    assert response.json()["code"] == "media_too_large"
    assert not MediaBlobRecord.objects.exists()


def test_upload_of_exactly_limit_is_accepted(settings: Settings) -> None:
    data = _png()
    settings.STRATA_MEDIA_MAX_UPLOAD_BYTES = len(data)

    response = _uploader().post(
        reverse(URL_NAME), {"file": _file(data)}, format="multipart"
    )

    assert response.status_code == 201


def test_oversized_content_length_rejected_before_parsing(
    settings: Settings,
) -> None:
    settings.STRATA_MEDIA_MAX_UPLOAD_BYTES = 1_000
    # Unparseable multipart: without the Content-Length guard this would be
    # a DRF parse error without a media `code`.
    response = _uploader().generic(
        "POST",
        reverse(URL_NAME),
        data="x" * (1_000 + 64 * 1024 + 1),
        content_type="multipart/form-data; boundary=never-parsed",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "media_too_large"


def test_content_length_guard_requires_permission_first(
    settings: Settings,
) -> None:
    settings.STRATA_MEDIA_MAX_UPLOAD_BYTES = 1_000
    client = _client("change_contentrecord", username="no-upload-big-body")

    response = client.generic(
        "POST",
        reverse(URL_NAME),
        data="x" * (1_000 + 64 * 1024 + 1),
        content_type="multipart/form-data; boundary=never-parsed",
    )

    assert response.status_code == 403


def test_upload_corrupt_returns_400_not_500(media_root: Path) -> None:
    data = _png()

    response = _uploader().post(
        reverse(URL_NAME),
        {"file": _file(data[: len(data) // 2])},
        format="multipart",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "media_unreadable"
    assert not MediaBlobRecord.objects.exists()
    assert not [path for path in media_root.rglob("*") if path.is_file()]


def test_non_image_with_image_extension_rejected() -> None:
    response = _uploader().post(
        reverse(URL_NAME),
        {"file": _file(b"%PDF-1.4 fake", name="shark.jpg", content_type="image/jpeg")},
        format="multipart",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "media_unreadable"


def test_svg_rejected_even_with_preserve_original() -> None:
    response = _uploader().post(
        reverse(URL_NAME),
        {
            "file": _file(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "a.png"),
            "preserve_original": "true",
        },
        format="multipart",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "media_type_not_allowed"


def test_upload_requires_permission() -> None:
    client = _client("change_contentrecord", username="editor-no-upload")

    response = client.post(
        reverse(URL_NAME), {"file": _file(_png())}, format="multipart"
    )

    assert response.status_code == 403
    assert not MediaBlobRecord.objects.exists()


def test_upload_requires_authentication() -> None:
    response = APIClient().post(
        reverse(URL_NAME), {"file": _file(_png())}, format="multipart"
    )

    assert response.status_code in {401, 403}
    assert not MediaBlobRecord.objects.exists()


def test_upload_without_file_returns_400() -> None:
    response = _uploader().post(reverse(URL_NAME), {}, format="multipart")

    assert response.status_code == 400


def test_upload_rejects_json_body() -> None:
    response = _uploader().post(reverse(URL_NAME), {"file": "x"}, format="json")

    assert response.status_code == 415
