from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from strata_cms.application.content.commands import (
    CreateContent,
    CreateRevision,
    PublishContent,
)
from strata_cms.application.content.results import ContentWriteResult
from strata_cms.application.content.use_cases import (
    create_content,
    create_revision,
    publish_content,
)
from strata_cms.application.errors import ContentNotReadyError, InvalidContentDataError
from strata_cms.application.ports.authorization import ContentAction, ContentActor
from strata_cms.config import services
from strata_cms.contrib.media.apps import MediaPluginConfig
from strata_cms.contrib.media.schema import (
    MEDIA_CONTENT_TYPE,
    MEDIA_PLUGIN,
    MEDIA_TYPE,
    Attribution,
    MediaData,
    is_https_url,
    media_summary_title,
)
from strata_cms.domain.media import MediaBlob
from strata_cms.domain.value_objects import ActorId
from strata_cms.infrastructure.clock.system import DjangoClock
from strata_cms.infrastructure.ids.uuid import Uuid4Generator
from strata_cms.infrastructure.persistence.django.media import (
    DjangoMediaBlobRepository,
)
from strata_cms.infrastructure.persistence.django.models import RevisionRecord
from strata_cms.infrastructure.persistence.django.projections import (
    DjangoLinkTargetQueries,
)
from strata_cms.infrastructure.persistence.django.unit_of_work import (
    DjangoContentUnitOfWork,
)
from strata_cms.plugin_api import (
    BlobReference,
    ContentCapability,
    EditorInputKind,
    LinkPath,
    MarkdownText,
    ScalarEditorField,
)

ACTOR = ActorId("user:1")
ACTOR_CONTEXT = ContentActor(id=ACTOR, is_staff=True, permissions=frozenset())
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
COMPLETE_ATTRIBUTION = {
    "author": "Jane Diver",
    "license": "CC-BY-SA-4.0",
    "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    "source_url": "https://commons.wikimedia.org/wiki/File:Shark.jpg",
    "credit_line": None,
}


class _AllowAll:
    def authorize(self, actor: ContentActor, action: ContentAction) -> None:
        del actor, action


def _blob() -> UUID:
    blob_id = uuid4()
    DjangoMediaBlobRepository().add(
        MediaBlob(
            id=blob_id,
            storage_key=f"media/{blob_id}.png",
            sha256="a" * 64,
            content_type="image/png",
            size_bytes=10,
            width=3,
            height=2,
            created_at=NOW,
            created_by=ACTOR,
        )
    )
    return blob_id


def _data(blob_id: UUID, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "file": {"$blob": str(blob_id)},
        "title": "Whale shark",
        "alt_text": "A whale shark swimming near the surface",
        "decorative": False,
        "caption": None,
        "attribution": {},
        "requires_attribution": False,
    }
    data.update(overrides)
    return data


def _create(data: dict[str, object]) -> ContentWriteResult:
    return create_content(
        CreateContent(
            type_key=MEDIA_CONTENT_TYPE, schema_version=1, data=data, actor_id=ACTOR
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=services.get_content_type_service(),
        link_targets=DjangoLinkTargetQueries(),
        policy=_AllowAll(),
        actor=ACTOR_CONTEXT,
    )


def _revise(written: ContentWriteResult, data: dict[str, object]) -> ContentWriteResult:
    return create_revision(
        CreateRevision(
            content_id=written.content_id,
            expected_version=written.content_version,
            schema_version=1,
            data=data,
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=services.get_content_type_service(),
        link_targets=DjangoLinkTargetQueries(),
        policy=_AllowAll(),
        actor=ACTOR_CONTEXT,
    )


def _publish(written: ContentWriteResult) -> None:
    publish_content(
        PublishContent(
            content_id=written.content_id,
            revision_id=written.revision_id,
            expected_version=written.content_version,
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=services.get_content_type_service(),
        policy=_AllowAll(),
        actor=ACTOR_CONTEXT,
    )


def _not_ready_codes(written: ContentWriteResult) -> list[tuple[str, str | None]]:
    with pytest.raises(ContentNotReadyError) as raised:
        _publish(written)
    return [(problem.code, problem.path) for problem in raised.value.problems]


def _invalid(data: dict[str, object]) -> list[tuple[str, str | None]]:
    with pytest.raises(InvalidContentDataError) as raised:
        _create(data)
    return [(problem.code, problem.path) for problem in raised.value.problems]


def _value(
    *,
    title: str = "",
    alt_text: str = "",
    caption: MarkdownText | None = None,
    attribution: Attribution | None = None,
) -> MediaData:
    return MediaData(
        file=BlobReference(uuid4()),
        title=title,
        alt_text=alt_text,
        decorative=False,
        caption=caption,
        attribution=attribution or Attribution(),
        requires_attribution=False,
    )


def test_media_type_contract() -> None:
    assert str(MEDIA_PLUGIN.key) == "strata.media"
    assert MediaPluginConfig.strata_plugin is MEDIA_PLUGIN
    assert MEDIA_TYPE.key == MEDIA_CONTENT_TYPE
    assert str(MEDIA_TYPE.key) == "strata.media"
    assert MEDIA_TYPE.schema_version == 1
    assert MEDIA_TYPE.capabilities == frozenset(
        {
            ContentCapability.PUBLISHABLE,
            ContentCapability.PREVIEWABLE,
            ContentCapability.SEARCHABLE,
        }
    )
    assert [field.path for field in MEDIA_TYPE.blob_fields] == [LinkPath.parse("file")]
    assert MEDIA_TYPE.embed_in_references is True


def test_summary_title_falls_back_to_alt_text_then_placeholder() -> None:
    assert media_summary_title(_value(title="Title", alt_text="Alt")) == "Title"
    assert media_summary_title(_value(title="  ", alt_text="Alt")) == "Alt"
    assert media_summary_title(_value()) == "Untitled media"


def test_editor_declares_media_fields() -> None:
    assert MEDIA_TYPE.editor is not None
    kinds = {
        ".".join(field.path): field.input_kind
        for field in MEDIA_TYPE.editor.fields
        if isinstance(field, ScalarEditorField)
    }
    assert kinds == {
        "file": EditorInputKind.BLOB,
        "title": EditorInputKind.TEXT,
        "alt_text": EditorInputKind.TEXT,
        "decorative": EditorInputKind.BOOLEAN,
        "caption": EditorInputKind.MARKDOWN,
        "attribution.author": EditorInputKind.TEXT,
        "attribution.license": EditorInputKind.TEXT,
        "attribution.license_url": EditorInputKind.URL,
        "attribution.source_url": EditorInputKind.URL,
        "attribution.credit_line": EditorInputKind.TEXT,
        "requires_attribution": EditorInputKind.BOOLEAN,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://commons.wikimedia.org/wiki/File:X.jpg", True),
        ("HTTPS://example.org/a", True),
        ("http://example.org/a", False),
        ("ftp://example.org/a", False),
        ("javascript:alert(1)", False),
        ("not a url", False),
        ("https://", False),
        ("https:///path", False),
        ("https://exa mple.org/", False),
        ("https://user:pass@example.org/", False),
        ("https://[::1", False),
        ("//example.org/a", False),
        ("https://example.org/" + "a" * 2100, False),
    ],
)
def test_is_https_url(value: str, *, expected: bool) -> None:
    assert is_https_url(value) is expected


@pytest.mark.django_db
def test_media_draft_saves_without_alt_text() -> None:
    written = _create(_data(_blob(), alt_text="", title=""))

    stored = RevisionRecord.objects.get(pk=written.revision_id).data
    assert stored["alt_text"] == ""
    assert stored["attribution"] == dict.fromkeys(
        ("author", "license", "license_url", "source_url", "credit_line")
    )


@pytest.mark.django_db
def test_media_publish_rejected_without_alt_text() -> None:
    written = _create(_data(_blob(), alt_text="   "))

    assert _not_ready_codes(written) == [("alt_text_required", "$.alt_text")]


@pytest.mark.django_db
def test_decorative_media_publishes_without_alt() -> None:
    written = _create(_data(_blob(), alt_text="", decorative=True))

    _publish(written)


@pytest.mark.django_db
def test_requires_attribution_blocks_publish_until_complete() -> None:
    blob_id = _blob()
    written = _create(
        _data(
            blob_id,
            requires_attribution=True,
            attribution={"author": " ", "credit_line": "Photo: Jane"},
        )
    )

    assert _not_ready_codes(written) == [
        ("attribution_incomplete", "$.attribution.author"),
        ("attribution_incomplete", "$.attribution.license"),
        ("attribution_incomplete", "$.attribution.source_url"),
    ]

    complete = _revise(
        written,
        _data(blob_id, requires_attribution=True, attribution=COMPLETE_ATTRIBUTION),
    )
    _publish(complete)


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["source_url", "license_url"])
@pytest.mark.parametrize(
    "url", ["http://commons.wikimedia.org/x", "ftp://example.org/x", "junk"]
)
def test_http_source_url_rejected_on_write(field: str, url: str) -> None:
    problems = _invalid(_data(_blob(), attribution={field: url}))

    assert problems == [("attribution_url_not_https", f"$.attribution.{field}")]


@pytest.mark.django_db
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_attribution_urls_are_unset(blank: str | None) -> None:
    written = _create(
        _data(_blob(), attribution={"source_url": blank, "license_url": blank})
    )

    stored = RevisionRecord.objects.get(pk=written.revision_id).data
    assert stored["attribution"]["source_url"] is None
    assert stored["attribution"]["license_url"] is None


@pytest.mark.django_db
def test_caption_is_validated_and_normalized_markdown() -> None:
    assert _invalid(_data(_blob(), caption="<b>bold</b>")) == [
        ("markdown_html_not_allowed", "$.caption")
    ]

    written = _create(_data(_blob(), caption="Seen *off* Mozambique  \r\n"))
    blank = _create(_data(_blob(), caption="  \n"))

    assert RevisionRecord.objects.get(pk=written.revision_id).data["caption"] == (
        "Seen *off* Mozambique\n"
    )
    assert RevisionRecord.objects.get(pk=blank.revision_id).data["caption"] is None


@pytest.mark.django_db
def test_file_is_required_and_must_exist() -> None:
    assert _invalid(_data(_blob(), file=None)) == [("file_required", "$.file")]
    assert _invalid(_data(uuid4())) == [("blob_not_found", "$.file")]
    assert _invalid(_data(_blob(), file="not-a-link"))[0][0] == "invalid_schema"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "overrides",
    [
        {"title": 1},
        {"decorative": "yes"},
        {"caption": ["x"]},
        {"attribution": "Jane"},
        {"attribution": {"author": 3}},
        {"attribution": {"photographer": "Jane"}},
    ],
)
def test_wrongly_typed_fields_are_rejected(overrides: dict[str, object]) -> None:
    assert [code for code, _path in _invalid(_data(_blob(), **overrides))] == [
        "invalid_schema"
    ]


def test_validate_reports_both_bad_urls() -> None:
    value = _value(
        attribution=Attribution(license_url="http://a.example", source_url="b"),
        caption=MarkdownText("fine\n"),
    )

    assert [issue.path for issue in MEDIA_TYPE.schema.validate(value)] == [
        "$.attribution.license_url",
        "$.attribution.source_url",
    ]
