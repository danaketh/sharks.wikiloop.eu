import importlib
from io import BytesIO
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import clear_url_caches, reverse
from PIL import Image
from pytest_django.fixtures import Settings
from rest_framework.test import APIClient

from strata_cms.api.delivery import views as delivery_views
from strata_cms.api.management import views as management_views
from strata_cms.config import services, urls
from strata_cms.contrib.media.schema import (
    MEDIA_CONTENT_TYPE,
    MEDIA_PLUGIN,
    MEDIA_TYPE,
)
from strata_cms.domain.revision import RevisionData
from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.infrastructure.content_types import RegistryContentTypeService
from strata_cms.plugin_api import (
    ContentTypeDefinition,
    LinkPath,
    PluginDefinition,
    PluginKey,
    PluginRequirement,
    ReferenceFieldDefinition,
    StrataRegistryBuilder,
    ValidationIssue,
)

pytestmark = pytest.mark.django_db

_SPECIES = ContentTypeKey("tests.species")
_ATTRIBUTION = {
    "author": "Jane Diver",
    "license": "CC-BY-SA-4.0",
    "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    "source_url": "https://commons.wikimedia.org/wiki/File:Whale_shark.jpg",
    "credit_line": "Jane Diver / CC BY-SA 4.0",
}


class _DictSchema:
    value_type: type[dict[str, object]] = dict

    def deserialize(self, data: RevisionData) -> dict[str, object]:
        return data.as_dict()

    def serialize(self, value: dict[str, object]) -> RevisionData:
        return RevisionData.from_mapping(value)

    def validate(self, value: dict[str, object]) -> tuple[ValidationIssue, ...]:
        del value
        return ()


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL = "/media/"
    return tmp_path


@pytest.fixture
def content_types(monkeypatch: pytest.MonkeyPatch) -> RegistryContentTypeService:
    """Installed media type plus a species type that references media."""
    builder = StrataRegistryBuilder()
    builder.register_plugin(MEDIA_PLUGIN)
    builder.registrar_for(MEDIA_PLUGIN.key).register_content_type(MEDIA_TYPE)
    plugin = PluginDefinition(
        key=PluginKey("tests.species"),
        version="1.0.0",
        requires_strata=">=0.1,<0.2",
        requires_plugins=(PluginRequirement(MEDIA_PLUGIN.key),),
    )
    builder.register_plugin(plugin)
    builder.registrar_for(plugin.key).register_content_type(
        ContentTypeDefinition(
            key=_SPECIES,
            schema_version=1,
            schema=_DictSchema(),
            reference_fields=(
                ReferenceFieldDefinition(
                    LinkPath.parse("photo"), frozenset({MEDIA_CONTENT_TYPE})
                ),
            ),
        )
    )
    service = RegistryContentTypeService(builder.build(strata_version="0.1.0"))
    for module in (services, delivery_views, management_views):
        monkeypatch.setattr(module, "get_content_type_service", lambda: service)
    return service


def _editor() -> APIClient:
    user = get_user_model().objects.create_superuser(
        username="editor", email="editor@example.invalid", password="test-password"
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _upload(client: APIClient) -> dict[str, object]:
    buffer = BytesIO()
    Image.new("RGB", (6, 4), (0, 80, 160)).save(buffer, "PNG")
    response = client.post(
        reverse("api:management:media-blob-upload"),
        {"file": SimpleUploadedFile("shark.png", buffer.getvalue())},
        format="multipart",
    )
    assert response.status_code == 201, response.content
    body: dict[str, object] = response.json()
    return body


def _create_and_publish(
    client: APIClient, type_key: ContentTypeKey, data: dict[str, object]
) -> str:
    created = client.post(
        reverse("api:management:content-create"),
        {"type": str(type_key), "schema_version": 1, "data": data},
        format="json",
    )
    assert created.status_code == 201, created.content
    body = created.json()
    published = client.post(
        reverse("api:management:content-publish", args=[body["content_id"]]),
        {"expected_version": 1, "revision_id": body["revision_id"]},
        format="json",
    )
    assert published.status_code == 200, published.content
    content_id: str = body["content_id"]
    return content_id


def _media_data(blob_id: object) -> dict[str, object]:
    return {
        "file": {"$blob": blob_id},
        "title": "Whale shark",
        "alt_text": "A whale shark near the surface",
        "decorative": False,
        "caption": "Off *Mozambique*.",
        "attribution": _ATTRIBUTION,
        "requires_attribution": True,
    }


def _expected_media(blob: dict[str, object]) -> dict[str, object]:
    hex_id = str(blob["blob_id"]).replace("-", "")
    return {
        "file": {
            "url": f"/media/media/{hex_id[0:2]}/{hex_id[2:4]}/{blob['blob_id']}.png",
            "content_type": "image/png",
            "width": 6,
            "height": 4,
            "size_bytes": blob["size_bytes"],
        },
        "title": "Whale shark",
        "alt_text": "A whale shark near the surface",
        "decorative": False,
        "caption": "Off *Mozambique*.\n",
        "attribution": _ATTRIBUTION,
        "requires_attribution": True,
    }


def test_media_delivery_payload_includes_url_dimensions_and_attribution(
    content_types: RegistryContentTypeService,
) -> None:
    del content_types
    editor = _editor()
    blob = _upload(editor)
    media_id = _create_and_publish(
        editor, MEDIA_CONTENT_TYPE, _media_data(blob["blob_id"])
    )

    response = APIClient().get(reverse("api:delivery:content-detail", args=[media_id]))

    assert response.status_code == 200
    assert response.json()["data"] == _expected_media(blob)
    assert "storage_key" not in response.content.decode()
    assert "sha256" not in response.content.decode()


def test_media_with_missing_alt_text_cannot_be_published_via_api(
    content_types: RegistryContentTypeService,
) -> None:
    del content_types
    editor = _editor()
    blob = _upload(editor)
    created = editor.post(
        reverse("api:management:content-create"),
        {
            "type": str(MEDIA_CONTENT_TYPE),
            "schema_version": 1,
            "data": {**_media_data(blob["blob_id"]), "alt_text": ""},
        },
        format="json",
    )
    assert created.status_code == 201
    body = created.json()

    published = editor.post(
        reverse("api:management:content-publish", args=[body["content_id"]]),
        {"expected_version": 1, "revision_id": body["revision_id"]},
        format="json",
    )

    assert published.status_code == 422
    assert "alt_text_required" in published.content.decode()


def test_end_to_end_upload_create_publish_reference_deliver(
    content_types: RegistryContentTypeService,
) -> None:
    del content_types
    editor = _editor()
    blob = _upload(editor)
    media_id = _create_and_publish(
        editor, MEDIA_CONTENT_TYPE, _media_data(blob["blob_id"])
    )
    species_id = _create_and_publish(
        editor,
        _SPECIES,
        {"name": "Rhincodon typus", "photo": {"$ref": media_id}},
    )

    response = APIClient().get(
        reverse("api:delivery:content-detail", args=[species_id])
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "name": "Rhincodon typus",
        "photo": {
            "id": media_id,
            "type": "strata.media",
            "title": "Whale shark",
            "path": None,
            "data": _expected_media(blob),
        },
    }


@pytest.mark.parametrize("debug", [True, False])
def test_media_url_is_served_only_in_debug(settings: Settings, *, debug: bool) -> None:
    settings.DEBUG = debug
    try:
        reloaded = importlib.reload(urls)
        prefixes = [str(pattern.pattern) for pattern in reloaded.urlpatterns]
    finally:
        settings.DEBUG = False
        importlib.reload(urls)
        clear_url_caches()

    assert any(prefix.startswith("^media/") for prefix in prefixes) is debug
