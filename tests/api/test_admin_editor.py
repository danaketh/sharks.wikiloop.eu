import json
import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import strata_cms.admin
from strata_cms.admin import admin as admin_module
from strata_cms.contrib.media.schema import (
    MEDIA_CONTENT_TYPE,
    MEDIA_PLUGIN,
    MEDIA_TYPE,
)
from strata_cms.domain.revision import RevisionData
from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.examples.simple_article import EXAMPLE_CONTENT_TYPE
from strata_cms.infrastructure.editors.registry import RegistryEditorCatalog
from strata_cms.infrastructure.persistence.django.models import ContentRecord
from strata_cms.plugin_api import (
    BlobFieldDefinition,
    ContentEditorDefinition,
    ContentTypeDefinition,
    EditorInputKind,
    LinkPath,
    PluginDefinition,
    PluginKey,
    PluginRequirement,
    ReferenceFieldDefinition,
    ScalarEditorField,
    StrataRegistryBuilder,
    ValidationIssue,
)

pytestmark = pytest.mark.django_db

_SHARK = ContentTypeKey("tests.shark")
_EDITOR_JS = Path(strata_cms.admin.__file__).parent / "static" / "strata" / "editor.js"


def _admin_client() -> Client:
    user_model = get_user_model()
    user = user_model.objects.create_superuser(
        username="admin",
        email="admin@example.invalid",
        password="test-password",
    )
    client = Client()
    client.force_login(user)
    return client


def _json_script(content: bytes, element_id: str) -> object:
    match = re.search(
        rf'<script id="{element_id}" type="application/json">(.*?)</script>',
        content.decode(),
        re.DOTALL,
    )
    assert match is not None, f"missing json_script {element_id}"
    return json.loads(match.group(1))


def _endpoints(content: bytes) -> dict[str, object]:
    endpoints = _json_script(content, "cms-editor-endpoints")
    assert isinstance(endpoints, dict)
    return endpoints


class _DictSchema:
    value_type: type[dict[str, object]] = dict

    def deserialize(self, data: RevisionData) -> dict[str, object]:
        return data.as_dict()

    def serialize(self, value: dict[str, object]) -> RevisionData:
        return RevisionData.from_mapping(value)

    def validate(self, value: dict[str, object]) -> tuple[ValidationIssue, ...]:
        del value
        return ()


def _shark_catalog(*, with_media: bool) -> RegistryEditorCatalog:
    """Build a type using markdown, single/many references and a blob."""
    builder = StrataRegistryBuilder()
    requirements: tuple[PluginRequirement, ...] = ()
    targets = frozenset({_SHARK})
    if with_media:
        builder.register_plugin(MEDIA_PLUGIN)
        builder.registrar_for(MEDIA_PLUGIN.key).register_content_type(MEDIA_TYPE)
        requirements = (PluginRequirement(MEDIA_PLUGIN.key),)
        targets = frozenset({_SHARK, MEDIA_CONTENT_TYPE})
    plugin = PluginDefinition(
        key=PluginKey("tests.sharks"),
        version="1.0.0",
        requires_strata=">=0.1,<0.2",
        requires_plugins=requirements,
    )
    builder.register_plugin(plugin)
    builder.registrar_for(plugin.key).register_content_type(
        ContentTypeDefinition(
            key=_SHARK,
            schema_version=1,
            schema=_DictSchema(),
            summary_title=lambda value: str(value.get("name", "")),
            reference_fields=(
                ReferenceFieldDefinition(LinkPath.parse("photo"), targets),
                ReferenceFieldDefinition(LinkPath.parse("related"), targets, many=True),
            ),
            blob_fields=(BlobFieldDefinition(LinkPath.parse("diagram")),),
            editor=ContentEditorDefinition(
                label="Shark",
                initial_data=RevisionData.from_mapping(
                    {"bio": "", "photo": None, "related": [], "diagram": None}
                ),
                fields=(
                    ScalarEditorField(
                        path=("bio",), label="Bio", input_kind=EditorInputKind.MARKDOWN
                    ),
                    ScalarEditorField(
                        path=("photo",),
                        label="Photo",
                        input_kind=EditorInputKind.REFERENCE,
                        reference_types=tuple(sorted(targets, key=str)),
                        required=False,
                    ),
                    ScalarEditorField(
                        path=("related",),
                        label="Related",
                        input_kind=EditorInputKind.REFERENCE,
                        reference_types=tuple(sorted(targets, key=str)),
                        required=False,
                    ),
                    ScalarEditorField(
                        path=("diagram",),
                        label="Diagram",
                        input_kind=EditorInputKind.BLOB,
                        required=False,
                    ),
                ),
            ),
        )
    )
    return RegistryEditorCatalog(builder.build(strata_version="0.1.0"))


def _use_catalog(
    monkeypatch: pytest.MonkeyPatch, catalog: RegistryEditorCatalog
) -> None:
    monkeypatch.setattr(admin_module, "get_editor_catalog", lambda: catalog)


def test_admin_add_view_uses_content_type_chooser_and_schema_editor() -> None:
    client = _admin_client()
    url = reverse("admin:strata_persistence_contentrecord_add")

    chooser = client.get(url)
    editor = client.get(url, {"type": str(EXAMPLE_CONTENT_TYPE)})

    assert chooser.status_code == 200
    assert b"Choose content type" in chooser.content
    assert editor.status_code == 200
    assert b"cms-editor-schema" in editor.content
    assert b"Save revision" in editor.content


def test_admin_change_redirects_away_from_django_model_form() -> None:
    client = _admin_client()
    record = ContentRecord.objects.create(
        type_key=str(EXAMPLE_CONTENT_TYPE),
        created_at=timezone.now(),
        created_by="test",
    )
    change_url = reverse(
        "admin:strata_persistence_contentrecord_change",
        args=[record.pk],
    )

    response = client.get(change_url)

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/{record.pk}/editor/")


def test_editor_page_includes_markdown_preview_url() -> None:
    client = _admin_client()

    response = client.get(
        reverse("admin:strata_persistence_contentrecord_add"),
        {"type": str(MEDIA_CONTENT_TYPE)},
    )

    assert response.status_code == 200
    endpoints = _endpoints(response.content)
    assert endpoints["markdown_preview"] == reverse("api:management:markdown-preview")


def test_editor_page_includes_picker_and_upload_urls() -> None:
    client = _admin_client()

    response = client.get(
        reverse("admin:strata_persistence_contentrecord_add"),
        {"type": str(MEDIA_CONTENT_TYPE)},
    )

    assert response.status_code == 200
    endpoints = _endpoints(response.content)
    assert endpoints["picker"] == reverse("api:management:content-create")
    assert endpoints["blob_upload"] == reverse("api:management:media-blob-upload")


def test_editor_page_shows_readiness_banner_container() -> None:
    client = _admin_client()
    created = client.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "Great white", "body": "Body"},
        },
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    content_id = created.json()["content_id"]

    response = client.get(
        reverse("admin:strata_persistence_contentrecord_editor", args=[content_id])
    )

    assert response.status_code == 200
    assert b'id="cms-editor-readiness"' in response.content
    endpoints = _endpoints(response.content)
    assert endpoints["document"] == reverse(
        "api:management:content-detail", args=[content_id]
    )


def test_new_content_editor_has_no_document_readiness_endpoint() -> None:
    client = _admin_client()

    response = client.get(
        reverse("admin:strata_persistence_contentrecord_add"),
        {"type": str(EXAMPLE_CONTENT_TYPE)},
    )

    assert response.status_code == 200
    assert _endpoints(response.content)["document"] is None


def test_editor_page_exposes_widget_contract_for_new_field_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_catalog(monkeypatch, _shark_catalog(with_media=True))
    client = _admin_client()

    response = client.get(
        reverse("admin:strata_persistence_contentrecord_add"),
        {"type": str(_SHARK)},
    )

    assert response.status_code == 200
    schema = _json_script(response.content, "cms-editor-schema")
    assert isinstance(schema, dict)
    fields = {tuple(field["path"]): field for field in schema["fields"]}
    assert fields[("bio",)]["input_kind"] == "markdown"
    assert fields[("diagram",)]["input_kind"] == "blob"
    photo = fields[("photo",)]
    assert photo["input_kind"] == "reference"
    assert photo["reference_types"] == ["strata.media", "tests.shark"]
    assert photo["reference_many"] is False
    related = fields[("related",)]
    assert related["input_kind"] == "reference"
    assert related["reference_types"] == ["strata.media", "tests.shark"]
    assert related["reference_many"] is True

    endpoints = _endpoints(response.content)
    assert endpoints["create"] == reverse("api:management:content-create")
    assert endpoints["picker"] == reverse("api:management:content-create")
    assert endpoints["markdown_preview"] == reverse("api:management:markdown-preview")
    assert endpoints["blob_upload"] == reverse("api:management:media-blob-upload")

    media = _json_script(response.content, "cms-editor-media")
    assert isinstance(media, dict)
    assert media["type"] == "strata.media"
    assert media["schema_version"] == MEDIA_TYPE.schema_version
    initial = media["initial_data"]
    assert isinstance(initial, dict)
    assert initial["file"] is None
    assert media["blob_path"] == ["file"]


def test_editor_page_omits_media_upload_when_media_type_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _shark_catalog(with_media=False)
    assert catalog.get_content_type(MEDIA_CONTENT_TYPE) is None
    _use_catalog(monkeypatch, catalog)
    client = _admin_client()

    response = client.get(
        reverse("admin:strata_persistence_contentrecord_add"),
        {"type": str(_SHARK)},
    )

    assert response.status_code == 200
    assert _json_script(response.content, "cms-editor-media") is None


def test_editor_script_only_uses_inner_html_for_server_preview() -> None:
    script = _EDITOR_JS.read_text(encoding="utf-8")

    assert script.count("innerHTML") == 1
    assert "preview.innerHTML = payload.html" in script
