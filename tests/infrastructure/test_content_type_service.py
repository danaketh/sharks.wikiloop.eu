from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from strata_cms.application.errors import (
    ContentTypeUnavailableError,
    InvalidContentDataError,
)
from strata_cms.application.ports.content_types import (
    ContentLinks,
    PreparedContentData,
)
from strata_cms.domain.revision import Revision, RevisionData
from strata_cms.domain.value_objects import (
    ActorId,
    ContentId,
    ContentTypeKey,
    RevisionId,
)
from strata_cms.examples.simple_article import (
    EXAMPLE_ARTICLE,
    EXAMPLE_CONTENT_TYPE,
    EXAMPLE_PLUGIN,
    ArticleData,
    register_example,
)
from strata_cms.examples.structured_page import LANDING_PAGE_TYPE
from strata_cms.infrastructure.content_types import RegistryContentTypeService
from strata_cms.plugin_api import (
    BlobFieldDefinition,
    BlockDefinition,
    BlockFieldDefinition,
    BlockSlotDefinition,
    BlockTypeKey,
    ContentTypeDefinition,
    LinkPath,
    PluginDefinition,
    PluginKey,
    ReferenceFieldDefinition,
    StrataRegistryBuilder,
    ValidationIssue,
)


def _service() -> RegistryContentTypeService:
    builder = StrataRegistryBuilder()
    register_example(builder)
    return RegistryContentTypeService(builder.build(strata_version="0.1.0"))


def test_prepare_for_write_upgrades_to_current_schema() -> None:
    prepared = _service().prepare_for_write(
        type_key=EXAMPLE_CONTENT_TYPE,
        schema_version=1,
        data={"heading": "Old title", "body": "Text"},
    )

    assert prepared.schema_version == 2
    assert prepared.data.as_dict() == {"title": "Old title", "body": "Text"}


def test_delivery_data_migrates_historical_revision() -> None:
    revision = Revision(
        id=RevisionId(UUID(int=2)),
        content_id=ContentId(UUID(int=1)),
        number=1,
        content_type=EXAMPLE_CONTENT_TYPE,
        schema_version=1,
        data=RevisionData.from_mapping({"heading": "Old title", "body": "Text"}),
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        created_by=ActorId("user:1"),
    )

    assert _service().delivery_data(revision) == {
        "title": "Old title",
        "body": "Text",
        "kind": "article",
    }


def test_prepare_for_write_translates_validation_errors() -> None:
    service = _service()

    with pytest.raises(InvalidContentDataError) as raised:
        service.prepare_for_write(
            type_key=EXAMPLE_CONTENT_TYPE,
            schema_version=2,
            data={"title": "  ", "body": "Text"},
        )

    assert raised.value.problems[0].code == "title_required"
    assert raised.value.problems[0].path == "$.title"


def test_prepare_for_write_translates_unknown_content_type() -> None:
    service = _service()

    with pytest.raises(ContentTypeUnavailableError) as raised:
        service.prepare_for_write(
            type_key=ContentTypeKey("tests.unknown"),
            schema_version=1,
            data={},
        )

    assert str(raised.value.type_key) == "tests.unknown"


def test_structured_content_write_normalizes_nested_block_versions() -> None:
    prepared = _service().prepare_for_write(
        type_key=LANDING_PAGE_TYPE,
        schema_version=1,
        data={
            "title": "Blocks",
            "body": [
                {
                    "id": "00000000-0000-0000-0000-000000000101",
                    "type": "strata_examples.text",
                    "version": 1,
                    "data": {"value": "Historical text"},
                    "slots": {},
                }
            ],
        },
    )

    body = prepared.data.as_dict()["body"]
    assert isinstance(body, list)
    assert body[0]["version"] == 2
    assert body[0]["data"] == {"text": "Historical text"}


def test_structured_content_delivery_uses_block_delivery_serializers() -> None:
    revision = Revision(
        id=RevisionId(UUID(int=12)),
        content_id=ContentId(UUID(int=11)),
        number=1,
        content_type=LANDING_PAGE_TYPE,
        schema_version=1,
        data=RevisionData.from_mapping(
            {
                "title": "Blocks",
                "body": [
                    {
                        "id": "00000000-0000-0000-0000-000000000101",
                        "type": "strata_examples.text",
                        "version": 1,
                        "data": {"value": "Historical text"},
                        "slots": {},
                    }
                ],
            }
        ),
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        created_by=ActorId("user:1"),
    )

    assert _service().delivery_data(revision) == {
        "title": "Blocks",
        "kind": "landing_page",
        "body": [
            {
                "id": "00000000-0000-0000-0000-000000000101",
                "type": "strata_examples.text",
                "data": {"text": "Historical text", "kind": "text"},
                "slots": {},
            }
        ],
    }


class _NonBlankBody:
    def publish_issues(self, value: ArticleData) -> tuple[ValidationIssue, ...]:
        if value.body.strip():
            return ()
        return (
            ValidationIssue(
                code="body_required_to_publish",
                message="Body is required to publish.",
                path="$.body",
            ),
        )


def test_publish_issues_evaluated_after_historical_migration() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(EXAMPLE_PLUGIN)
    registrar = builder.registrar_for(EXAMPLE_PLUGIN.key)
    registrar.register_content_type(replace(EXAMPLE_ARTICLE, readiness=_NonBlankBody()))
    service = RegistryContentTypeService(builder.build(strata_version="0.1.0"))
    revision = Revision(
        id=RevisionId(UUID(int=2)),
        content_id=ContentId(UUID(int=1)),
        number=1,
        content_type=EXAMPLE_CONTENT_TYPE,
        schema_version=1,
        data=RevisionData.from_mapping({"heading": "Old title", "body": ""}),
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        created_by=ActorId("user:1"),
    )

    problems = service.publish_issues(revision)

    assert [(p.code, p.path) for p in problems] == [
        ("body_required_to_publish", "$.body")
    ]
    service.validate_revision(revision)


def test_publish_issues_empty_without_readiness() -> None:
    revision = Revision(
        id=RevisionId(UUID(int=2)),
        content_id=ContentId(UUID(int=1)),
        number=1,
        content_type=EXAMPLE_CONTENT_TYPE,
        schema_version=2,
        data=RevisionData.from_mapping({"title": "T", "body": ""}),
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        created_by=ActorId("user:1"),
    )

    assert _service().publish_issues(revision) == ()


# --- link extraction and write-time link validation ---------------------------

_U = "00000000-0000-0000-0000-00000000000{}"
_TARGET = ContentTypeKey("tests.target")
_PAGE = ContentTypeKey("tests.page")
_IMAGE = BlockTypeKey("tests.image")


class _DictSchema:
    value_type: type[dict[str, object]] = dict

    def deserialize(self, data: RevisionData) -> dict[str, object]:
        return data.as_dict()

    def serialize(self, value: dict[str, object]) -> RevisionData:
        return RevisionData.from_mapping(value)

    def validate(self, value: dict[str, object]) -> tuple[ValidationIssue, ...]:
        del value
        return ()


def _title(value: dict[str, object]) -> str:
    return str(value.get("title", ""))


def _link_service(
    *,
    many: ReferenceFieldDefinition | None = None,
    embed: bool = False,
) -> RegistryContentTypeService:
    builder = StrataRegistryBuilder()
    plugin = PluginDefinition(
        key=PluginKey("tests.links"),
        version="1.0.0",
        requires_strata=">=0.1,<0.2",
    )
    builder.register_plugin(plugin)
    registrar = builder.registrar_for(plugin.key)
    registrar.register_content_type(
        ContentTypeDefinition(
            key=_TARGET,
            schema_version=1,
            schema=_DictSchema(),
            summary_title=_title,
            embed_in_references=embed,
        )
    )
    registrar.register_block_type(
        BlockDefinition(
            key=_IMAGE,
            schema_version=1,
            schema=_DictSchema(),
            reference_fields=(
                ReferenceFieldDefinition(LinkPath.parse("page"), frozenset({_TARGET})),
            ),
            blob_fields=(BlobFieldDefinition(LinkPath.parse("image")),),
            slots=(BlockSlotDefinition("children"),),
        )
    )
    references: tuple[ReferenceFieldDefinition, ...] = (
        ReferenceFieldDefinition(LinkPath.parse("hero"), frozenset({_TARGET})),
    )
    if many is not None:
        references += (many,)
    registrar.register_content_type(
        ContentTypeDefinition(
            key=_PAGE,
            schema_version=1,
            schema=_DictSchema(),
            reference_fields=references,
            blob_fields=(BlobFieldDefinition(LinkPath.parse("cover")),),
            block_fields=(BlockFieldDefinition(("body",), required=False),),
        )
    )
    return RegistryContentTypeService(builder.build(strata_version="0.1.0"))


def _prepare(
    service: RegistryContentTypeService, data: dict[str, object]
) -> PreparedContentData:
    return service.prepare_for_write(type_key=_PAGE, schema_version=1, data=data)


def _block(
    number: int, data: dict[str, object], children: list[object] | None = None
) -> dict[str, object]:
    return {
        "id": _U.format(number),
        "type": "tests.image",
        "version": 1,
        "data": data,
        "slots": {"children": children} if children is not None else {},
    }


def _problems(
    service: RegistryContentTypeService, data: dict[str, object]
) -> list[tuple[str, str]]:
    with pytest.raises(InvalidContentDataError) as raised:
        _prepare(service, data)
    return [(p.code, p.path) for p in raised.value.problems]


def test_extract_links_from_fields_and_blocks() -> None:
    service = _link_service()
    inner = _block(2, {"image": {"$blob": _U.format(6)}})
    data: dict[str, object] = {
        "hero": {"$ref": _U.format(3)},
        "cover": {"$blob": _U.format(4)},
        "body": [
            _block(
                1,
                {"page": {"$ref": _U.format(5)}, "image": {"$blob": _U.format(7)}},
                [inner],
            )
        ],
    }
    prepared = _prepare(service, data)

    links = service.extract_links(type_key=_PAGE, data=prepared.data)

    outer = _U.format(1)
    assert [(link.location, str(link.target)) for link in links.references] == [
        ("hero", _U.format(3)),
        (f"body/blocks/{outer}/data/page", _U.format(5)),
    ]
    assert [(link.location, str(link.target)) for link in links.blobs] == [
        ("cover", _U.format(4)),
        (f"body/blocks/{outer}/data/image", _U.format(7)),
        (
            f"body/blocks/{outer}/slots/children/blocks/{_U.format(2)}/data/image",
            _U.format(6),
        ),
    ]
    assert all(link.kind == "reference" for link in links.references)
    assert all(link.kind == "blob" for link in links.blobs)


def test_declared_block_links_accepted_including_nested_slot_child() -> None:
    service = _link_service()
    child = _block(2, {"page": {"$ref": _U.format(3)}})
    prepared = _prepare(
        service,
        {"body": [_block(1, {"image": {"$blob": _U.format(4)}}, [child])]},
    )

    assert prepared.data.as_dict()["body"]


def test_extract_links_carries_allowed_types() -> None:
    service = _link_service()
    prepared = _prepare(
        service,
        {
            "hero": {"$ref": _U.format(3)},
            "cover": {"$blob": _U.format(4)},
            "body": [_block(1, {"page": {"$ref": _U.format(5)}})],
        },
    )

    links = service.extract_links(type_key=_PAGE, data=prepared.data)

    assert [link.allowed_types for link in links.references] == [
        frozenset({_TARGET}),
        frozenset({_TARGET}),
    ]
    assert [link.allowed_types for link in links.blobs] == [frozenset()]


def test_prepare_rejects_undeclared_ref() -> None:
    service = _link_service()

    assert _problems(service, {"elsewhere": {"$ref": _U.format(1)}}) == [
        ("undeclared_link", "$.elsewhere")
    ]


def test_prepare_rejects_undeclared_link_in_block_data() -> None:
    service = _link_service()
    block = _block(1, {"other": {"$blob": _U.format(2)}})

    assert _problems(service, {"body": [block]}) == [
        ("undeclared_link", "$.body[0].data.other")
    ]


def test_prepare_rejects_undeclared_link_in_nested_slot_child() -> None:
    service = _link_service()
    child = _block(2, {"other": {"$ref": _U.format(3)}})
    block = _block(1, {}, [_block(4, {}), child])

    assert _problems(service, {"body": [block]}) == [
        ("undeclared_link", "$.body[0].slots.children[1].data.other")
    ]


def test_prepare_rejects_wrong_link_kind() -> None:
    service = _link_service()

    assert _problems(service, {"hero": {"$blob": _U.format(1)}}) == [
        ("wrong_link_kind", "$.hero")
    ]


def test_prepare_rejects_malformed_links() -> None:
    service = _link_service()
    block = _block(1, {"page": {"$ref": "not-a-uuid"}})

    assert _problems(
        service,
        {
            "hero": {"$ref": _U.format(1), "extra": 1},
            "cover": {"$blob": "NOT-CANONICAL"},
            "body": [block],
        },
    ) == [
        ("malformed_link", "$.hero"),
        ("malformed_link", "$.cover"),
        ("malformed_link", "$.body[0].data.page"),
    ]


def _many(
    *, min_items: int = 0, max_items: int | None = None
) -> ReferenceFieldDefinition:
    return ReferenceFieldDefinition(
        LinkPath.parse("related"),
        frozenset({_TARGET}),
        many=True,
        min_items=min_items,
        max_items=max_items,
    )


def _page_revision(data: Mapping[str, object]) -> Revision:
    return Revision(
        id=RevisionId(UUID(int=2)),
        content_id=ContentId(UUID(int=1)),
        number=1,
        content_type=_PAGE,
        schema_version=1,
        data=RevisionData.from_mapping(data),
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        created_by=ActorId("user:1"),
    )


def test_prepare_rejects_reference_count_above_max_items() -> None:
    service = _link_service(many=_many(max_items=2))
    refs = [{"$ref": _U.format(n)} for n in range(1, 4)]

    assert _problems(service, {"related": refs}) == [
        ("reference_count_out_of_range", "$.related")
    ]
    assert (
        _prepare(service, {"related": refs[:2]}).data.as_dict()["related"] == (refs[:2])
    )


def test_prepare_accepts_incomplete_drafts_with_unset_links() -> None:
    service = _link_service(many=_many(min_items=1))

    drafts: list[dict[str, object]] = [
        {"hero": None, "cover": None, "related": None},
        {},
        {"related": []},
    ]
    for data in drafts:
        prepared = _prepare(service, data)
        links = service.extract_links(type_key=_PAGE, data=prepared.data)
        assert links == ContentLinks()


def test_prepare_accepts_null_link_in_block_data() -> None:
    service = _link_service()
    prepared = _prepare(service, {"body": [_block(1, {"page": None, "image": None})]})

    links = service.extract_links(type_key=_PAGE, data=prepared.data)

    assert links == ContentLinks()


def test_prepare_rejects_null_element_in_reference_list() -> None:
    service = _link_service(many=_many())

    assert _problems(service, {"related": [None]}) == [
        ("malformed_link", "$.related[0]")
    ]


def test_min_items_is_a_publish_issue_not_a_write_error() -> None:
    service = _link_service(many=_many(min_items=2))
    one = [{"$ref": _U.format(1)}]

    cases: list[dict[str, object]] = [{}, {"related": None}, {"related": []}]
    cases.append({"related": one})
    for data in cases:
        problems = service.publish_issues(_page_revision(data))
        assert [(p.code, p.path) for p in problems] == [
            ("reference_count_out_of_range", "$.related")
        ]
    full = {"related": one * 2}
    assert service.publish_issues(_page_revision(full)) == ()


def test_publish_issues_include_block_many_min_items() -> None:
    block_many = ReferenceFieldDefinition(
        LinkPath.parse("items"), frozenset({_TARGET}), many=True, min_items=1
    )
    builder = StrataRegistryBuilder()
    plugin = PluginDefinition(
        key=PluginKey("tests.links"), version="1.0.0", requires_strata=">=0.1,<0.2"
    )
    builder.register_plugin(plugin)
    registrar = builder.registrar_for(plugin.key)
    registrar.register_content_type(
        ContentTypeDefinition(
            key=_TARGET, schema_version=1, schema=_DictSchema(), summary_title=_title
        )
    )
    registrar.register_block_type(
        BlockDefinition(
            key=_IMAGE,
            schema_version=1,
            schema=_DictSchema(),
            reference_fields=(block_many,),
            slots=(BlockSlotDefinition("children"),),
        )
    )
    registrar.register_content_type(
        ContentTypeDefinition(
            key=_PAGE,
            schema_version=1,
            schema=_DictSchema(),
            block_fields=(BlockFieldDefinition(("body",), required=False),),
        )
    )
    service = RegistryContentTypeService(builder.build(strata_version="0.1.0"))
    revision = _page_revision({"body": [_block(1, {}, [_block(2, {"items": []})])]})

    problems = service.publish_issues(revision)

    assert [(p.code, p.path) for p in problems] == [
        ("reference_count_out_of_range", "$.body[0].data.items"),
        ("reference_count_out_of_range", "$.body[0].slots.children[0].data.items"),
    ]


def test_summary_title_and_embed_flag() -> None:
    service = _link_service(embed=True)
    revision = Revision(
        id=RevisionId(UUID(int=2)),
        content_id=ContentId(UUID(int=1)),
        number=1,
        content_type=_TARGET,
        schema_version=1,
        data=RevisionData.from_mapping({"title": "Hello"}),
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        created_by=ActorId("user:1"),
    )

    assert service.summary_title(revision) == "Hello"
    assert service.embeds_in_references(_TARGET) is True
    assert service.embeds_in_references(_PAGE) is False


def test_summary_title_none_when_type_has_no_hook() -> None:
    revision = Revision(
        id=RevisionId(UUID(int=2)),
        content_id=ContentId(UUID(int=1)),
        number=1,
        content_type=EXAMPLE_CONTENT_TYPE,
        schema_version=2,
        data=RevisionData.from_mapping({"title": "T", "body": ""}),
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        created_by=ActorId("user:1"),
    )

    assert _service().summary_title(revision) is None


def test_blob_ids_from_historical_revision() -> None:
    service = _link_service()
    revision = Revision(
        id=RevisionId(UUID(int=2)),
        content_id=ContentId(UUID(int=1)),
        number=1,
        content_type=_PAGE,
        schema_version=1,
        data=RevisionData.from_mapping(
            {
                "cover": {"$blob": _U.format(4)},
                "body": [_block(1, {"image": {"$blob": _U.format(7)}})],
            }
        ),
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        created_by=ActorId("user:1"),
    )

    assert service.blob_ids(revision) == frozenset(
        {UUID(_U.format(4)), UUID(_U.format(7))}
    )


def test_prepare_rejects_non_list_for_many_reference_field() -> None:
    many = ReferenceFieldDefinition(
        LinkPath.parse("related"), frozenset({_TARGET}), many=True
    )

    assert _problems(_link_service(many=many), {"related": "nope"}) == [
        ("malformed_link", "$.related")
    ]
