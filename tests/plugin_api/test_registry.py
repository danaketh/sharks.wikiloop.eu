from dataclasses import dataclass

import pytest

from strata_cms.domain.revision import RevisionData
from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.examples.simple_article import (
    EXAMPLE_ARTICLE,
    EXAMPLE_CONTENT_TYPE,
    EXAMPLE_PLUGIN,
    ArticleData,
    register_example,
)
from strata_cms.plugin_api import (
    BlobFieldDefinition,
    BlockDefinition,
    BlockFieldDefinition,
    BlockSlotDefinition,
    BlockTypeKey,
    ContentTypeDefinition,
    ContentValidationError,
    EditorInputKind,
    InvalidSchemaMigrationError,
    LinkPath,
    PluginDefinition,
    PluginKey,
    PluginRequirement,
    ReferenceFieldDefinition,
    ScalarEditorField,
    SchemaMigration,
    SchemaMigrationSet,
    StrataRegistryBuilder,
)
from strata_cms.plugin_api.errors import (
    ContentTypeNotFoundError,
    DuplicateContentTypeError,
    DuplicatePluginError,
    IncompatibleCMSVersionError,
    IncompatiblePluginVersionError,
    MissingPluginDependencyError,
    PluginDependencyCycleError,
    ReferenceTargetWithoutTitleError,
    RegistryConfigurationError,
    RegistryFrozenError,
    SchemaDecodeError,
    UndeclaredPluginDependencyError,
    UnknownPluginOwnerError,
    UnknownReferenceTargetTypeError,
    UnsupportedPluginAPIVersionError,
    UnsupportedSchemaVersionError,
)
from strata_cms.plugin_api.schema import ContentSchema, ValidationIssue


def _plugin(
    key: str,
    *,
    version: str = "1.0.0",
    requires: tuple[PluginRequirement, ...] = (),
    requires_strata: str = ">=0.1,<0.2",
    api_version: int = 1,
) -> PluginDefinition:
    return PluginDefinition(
        key=PluginKey(key),
        version=version,
        requires_strata=requires_strata,
        requires_plugins=requires,
        plugin_api_version=api_version,
    )


def test_example_migrates_v1_to_current_typed_value() -> None:
    value = EXAMPLE_ARTICLE.decode(
        schema_version=1,
        data=RevisionData.from_mapping({"heading": "Historical", "body": "Body"}),
    )

    assert value == ArticleData(title="Historical", body="Body")


def test_example_delivery_serializes_current_shape_after_migration() -> None:
    payload = EXAMPLE_ARTICLE.delivery_data(
        schema_version=1,
        data=RevisionData.from_mapping({"heading": "Historical", "body": "Body"}),
    )

    assert payload == {
        "title": "Historical",
        "body": "Body",
        "kind": "article",
    }


def test_example_rejects_semantically_invalid_content() -> None:
    with pytest.raises(ContentValidationError):
        EXAMPLE_ARTICLE.decode(
            schema_version=2,
            data=RevisionData.from_mapping({"title": "  ", "body": "Body"}),
        )


def test_content_type_rejects_future_schema_version() -> None:
    with pytest.raises(UnsupportedSchemaVersionError):
        EXAMPLE_ARTICLE.decode(
            schema_version=3,
            data=RevisionData.from_mapping({"title": "Future", "body": "Body"}),
        )


def test_registry_is_dependency_ordered_and_immutable_after_build() -> None:
    core = _plugin("tests.core")
    extension = _plugin(
        "tests.extension",
        requires=(PluginRequirement(core.key, ">=1,<2"),),
    )
    builder = StrataRegistryBuilder()
    builder.register_plugin(extension)
    builder.register_plugin(core)

    registry = builder.build(strata_version="0.1.0")

    assert [str(plugin.key) for plugin in registry.plugins.all()] == [
        "tests.core",
        "tests.extension",
    ]
    with pytest.raises(RegistryFrozenError):
        builder.register_plugin(_plugin("tests.late"))


def test_registry_exposes_content_owner_and_stable_lookup() -> None:
    builder = StrataRegistryBuilder()
    register_example(builder)

    registry = builder.build(strata_version="0.1.0")
    entry = registry.content_types.require(EXAMPLE_CONTENT_TYPE)

    assert entry.owner == EXAMPLE_PLUGIN.key
    assert entry.definition is EXAMPLE_ARTICLE


def test_registry_rejects_unknown_content_type() -> None:
    builder = StrataRegistryBuilder()
    register_example(builder)
    registry = builder.build(strata_version="0.1.0")

    with pytest.raises(ContentTypeNotFoundError):
        registry.content_types.require(ContentTypeKey("tests.unknown"))


def test_registry_rejects_duplicate_plugin_key() -> None:
    builder = StrataRegistryBuilder()
    plugin = _plugin("tests.plugin")
    builder.register_plugin(plugin)

    with pytest.raises(DuplicatePluginError):
        builder.register_plugin(plugin)


def test_registry_rejects_duplicate_content_type_key() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(EXAMPLE_PLUGIN)
    registrar = builder.registrar_for(EXAMPLE_PLUGIN.key)
    registrar.register_content_type(EXAMPLE_ARTICLE)

    with pytest.raises(DuplicateContentTypeError):
        registrar.register_content_type(EXAMPLE_ARTICLE)


def test_registry_rejects_missing_plugin_dependency() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(
        _plugin(
            "tests.extension",
            requires=(PluginRequirement(PluginKey("tests.missing")),),
        )
    )

    with pytest.raises(MissingPluginDependencyError):
        builder.build(strata_version="0.1.0")


def test_registry_rejects_incompatible_plugin_dependency() -> None:
    core = _plugin("tests.core", version="1.0.0")
    extension = _plugin(
        "tests.extension",
        requires=(PluginRequirement(core.key, ">=2"),),
    )
    builder = StrataRegistryBuilder()
    builder.register_plugin(core)
    builder.register_plugin(extension)

    with pytest.raises(IncompatiblePluginVersionError):
        builder.build(strata_version="0.1.0")


def test_registry_rejects_circular_plugin_dependencies() -> None:
    first_key = PluginKey("tests.first")
    second_key = PluginKey("tests.second")
    builder = StrataRegistryBuilder()
    builder.register_plugin(
        _plugin(
            str(first_key),
            requires=(PluginRequirement(second_key),),
        )
    )
    builder.register_plugin(
        _plugin(
            str(second_key),
            requires=(PluginRequirement(first_key),),
        )
    )

    with pytest.raises(PluginDependencyCycleError):
        builder.build(strata_version="0.1.0")


def test_registry_rejects_incompatible_strata_version() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(_plugin("tests.future", requires_strata=">=9"))

    with pytest.raises(IncompatibleCMSVersionError):
        builder.build(strata_version="0.1.0")


def test_registry_rejects_unsupported_plugin_api() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(_plugin("tests.future", api_version=2))

    with pytest.raises(UnsupportedPluginAPIVersionError):
        builder.build(strata_version="0.1.0")


def test_content_type_requires_complete_migration_chain() -> None:
    @dataclass(frozen=True)
    class Value:
        name: str

    class Schema(ContentSchema[Value]):
        value_type = Value

        def deserialize(self, data: RevisionData) -> Value:
            return Value(str(data.as_dict()["name"]))

        def serialize(self, value: Value) -> RevisionData:
            return RevisionData.from_mapping({"name": value.name})

        def validate(self, value: Value) -> tuple[ValidationIssue, ...]:
            del value
            return ()

    def migrate(data: RevisionData) -> RevisionData:
        return data

    with pytest.raises(InvalidSchemaMigrationError):
        ContentTypeDefinition(
            key=ContentTypeKey("tests.value"),
            schema_version=3,
            schema=Schema(),
            migrations=SchemaMigrationSet((SchemaMigration(1, 2, migrate),)),
        )


def test_registrar_is_scoped_to_registered_plugin_and_freezes_with_builder() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(EXAMPLE_PLUGIN)
    registrar = builder.registrar_for(EXAMPLE_PLUGIN.key)
    registrar.register_content_type(EXAMPLE_ARTICLE)
    builder.build(strata_version="0.1.0")

    with pytest.raises(RegistryFrozenError):
        registrar.register_content_type(EXAMPLE_ARTICLE)


def test_registrar_rejects_unknown_owner() -> None:
    builder = StrataRegistryBuilder()

    with pytest.raises(UnknownPluginOwnerError):
        builder.registrar_for(PluginKey("tests.unknown"))


def test_plugin_rejects_duplicate_dependency_declarations() -> None:
    dependency = PluginKey("tests.core")

    with pytest.raises(RegistryConfigurationError):
        _plugin(
            "tests.extension",
            requires=(
                PluginRequirement(dependency, ">=1"),
                PluginRequirement(dependency, "<2"),
            ),
        )


# --- typed reference and blob links -------------------------------------------

_U = "00000000-0000-0000-0000-00000000000{}"


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


def _content(
    key: str,
    *,
    refs: tuple[ReferenceFieldDefinition, ...] = (),
    titled: bool = True,
) -> ContentTypeDefinition[dict[str, object]]:
    return ContentTypeDefinition(
        key=ContentTypeKey(key),
        schema_version=1,
        schema=_DictSchema(),
        reference_fields=refs,
        summary_title=_title if titled else None,
    )


def _ref(path: str, *targets: str) -> ReferenceFieldDefinition:
    return ReferenceFieldDefinition(
        LinkPath.parse(path), frozenset(ContentTypeKey(t) for t in targets)
    )


def test_reference_to_unregistered_type_fails_compile() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(_plugin("tests.a"))
    builder.registrar_for(PluginKey("tests.a")).register_content_type(
        _content("tests.page", refs=(_ref("link", "tests.missing"),))
    )

    with pytest.raises(UnknownReferenceTargetTypeError):
        builder.build(strata_version="0.1.0")


def test_reference_to_type_without_summary_title_fails_compile() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(_plugin("tests.a"))
    registrar = builder.registrar_for(PluginKey("tests.a"))
    registrar.register_content_type(_content("tests.target", titled=False))
    registrar.register_content_type(
        _content("tests.page", refs=(_ref("link", "tests.target"),))
    )

    with pytest.raises(ReferenceTargetWithoutTitleError):
        builder.build(strata_version="0.1.0")


def test_reference_to_titled_type_compiles() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(_plugin("tests.a"))
    registrar = builder.registrar_for(PluginKey("tests.a"))
    registrar.register_content_type(_content("tests.target"))
    registrar.register_content_type(
        _content("tests.page", refs=(_ref("link", "tests.target"),))
    )

    builder.build(strata_version="0.1.0")
    assert _content("tests.target").summary_title_untyped({"title": "T"}) == "T"
    assert _content("tests.x", titled=False).summary_title_untyped({}) is None


def test_reference_to_other_plugin_type_requires_dependency() -> None:
    def build(*, declare: bool) -> None:
        builder = StrataRegistryBuilder()
        core = _plugin("tests.core")
        requires = (PluginRequirement(core.key, ">=1,<2"),) if declare else ()
        builder.register_plugin(core)
        builder.register_plugin(_plugin("tests.ext", requires=requires))
        builder.registrar_for(core.key).register_content_type(_content("tests.target"))
        builder.registrar_for(PluginKey("tests.ext")).register_content_type(
            _content("tests.page", refs=(_ref("link", "tests.target"),))
        )
        builder.build(strata_version="0.1.0")

    with pytest.raises(UndeclaredPluginDependencyError):
        build(declare=False)
    build(declare=True)


def test_duplicate_link_paths_rejected() -> None:
    with pytest.raises(RegistryConfigurationError):
        _content(
            "tests.page",
            refs=(_ref("link", "tests.t"), _ref("link", "tests.t")),
        )


def test_block_links_collected_with_block_uuid_location() -> None:
    block = BlockDefinition(
        key=BlockTypeKey("tests.image"),
        schema_version=1,
        schema=_DictSchema(),
        reference_fields=(_ref("page", "tests.target"),),
        blob_fields=(BlobFieldDefinition(LinkPath.parse("image")),),
        slots=(BlockSlotDefinition("children"),),
    )
    builder = StrataRegistryBuilder()
    builder.register_plugin(_plugin("tests.a"))
    registrar = builder.registrar_for(PluginKey("tests.a"))
    registrar.register_block_type(block)
    registrar.register_content_type(_content("tests.target"))
    registry = builder.build(strata_version="0.1.0")
    outer, inner = _U.format(1), _U.format(2)
    child = {
        "id": inner,
        "type": "tests.image",
        "version": 1,
        "data": {"image": {"$blob": _U.format(5)}},
        "slots": {},
    }
    data = RevisionData.from_mapping(
        {
            "body": [
                {
                    "id": outer,
                    "type": "tests.image",
                    "version": 1,
                    "data": {
                        "image": {"$blob": _U.format(3)},
                        "page": {"$ref": _U.format(4)},
                    },
                    "slots": {"children": [child]},
                }
            ]
        }
    )

    found = registry.blocks.collect_links(data, (BlockFieldDefinition(("body",)),))

    assert [(o.kind, o.location, str(o.target)) for o in found] == [
        ("reference", f"body/blocks/{outer}/data/page", _U.format(4)),
        ("blob", f"body/blocks/{outer}/data/image", _U.format(3)),
        (
            "blob",
            f"body/blocks/{outer}/slots/children/blocks/{inner}/data/image",
            _U.format(5),
        ),
    ]


def test_reference_editor_field_requires_reference_types() -> None:
    with pytest.raises(RegistryConfigurationError):
        ScalarEditorField(
            path=("link",), label="Link", input_kind=EditorInputKind.REFERENCE
        )
    with pytest.raises(RegistryConfigurationError):
        ScalarEditorField(
            path=("link",),
            label="Link",
            reference_types=(ContentTypeKey("tests.target"),),
        )
    field = ScalarEditorField(
        path=("link",),
        label="Link",
        input_kind=EditorInputKind.REFERENCE,
        reference_types=(ContentTypeKey("tests.target"),),
    )
    assert field.reference_types == (ContentTypeKey("tests.target"),)
    assert EditorInputKind.BLOB.value == "blob"


def test_summary_title_untyped_rejects_wrong_value_type() -> None:
    with pytest.raises(SchemaDecodeError):
        _content("tests.page").summary_title_untyped("not a dict")


def test_block_many_reference_links_collected_per_item() -> None:
    block = BlockDefinition(
        key=BlockTypeKey("tests.related"),
        schema_version=1,
        schema=_DictSchema(),
        reference_fields=(
            ReferenceFieldDefinition(
                LinkPath.parse("items"),
                frozenset({ContentTypeKey("tests.target")}),
                many=True,
            ),
        ),
    )
    builder = StrataRegistryBuilder()
    builder.register_plugin(_plugin("tests.a"))
    registrar = builder.registrar_for(PluginKey("tests.a"))
    registrar.register_block_type(block)
    registrar.register_content_type(_content("tests.target"))
    registry = builder.build(strata_version="0.1.0")
    data = RevisionData.from_mapping(
        {
            "body": [
                {
                    "id": _U.format(1),
                    "type": "tests.related",
                    "version": 1,
                    "data": {"items": [{"$ref": _U.format(2)}, {"$ref": _U.format(3)}]},
                    "slots": {},
                }
            ]
        }
    )

    found = registry.blocks.collect_links(data, (BlockFieldDefinition(("body",)),))

    assert [(o.location, str(o.target)) for o in found] == [
        (f"body/blocks/{_U.format(1)}/data/items/0", _U.format(2)),
        (f"body/blocks/{_U.format(1)}/data/items/1", _U.format(3)),
    ]


def _block_with_ref(target: str) -> BlockDefinition[dict[str, object]]:
    return BlockDefinition(
        key=BlockTypeKey("tests.linker"),
        schema_version=1,
        schema=_DictSchema(),
        reference_fields=(_ref("page", target),),
    )


def test_block_reference_to_unregistered_type_fails_compile() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(_plugin("tests.a"))
    builder.registrar_for(PluginKey("tests.a")).register_block_type(
        _block_with_ref("tests.missing")
    )

    with pytest.raises(UnknownReferenceTargetTypeError):
        builder.build(strata_version="0.1.0")


def test_block_reference_to_untitled_type_fails_compile() -> None:
    builder = StrataRegistryBuilder()
    builder.register_plugin(_plugin("tests.a"))
    registrar = builder.registrar_for(PluginKey("tests.a"))
    registrar.register_content_type(_content("tests.target", titled=False))
    registrar.register_block_type(_block_with_ref("tests.target"))

    with pytest.raises(ReferenceTargetWithoutTitleError):
        builder.build(strata_version="0.1.0")


def test_block_reference_to_other_plugin_type_requires_dependency() -> None:
    def build(*, declare: bool) -> None:
        builder = StrataRegistryBuilder()
        core = _plugin("tests.core")
        requires = (PluginRequirement(core.key, ">=1,<2"),) if declare else ()
        builder.register_plugin(core)
        builder.register_plugin(_plugin("tests.ext", requires=requires))
        builder.registrar_for(core.key).register_content_type(_content("tests.target"))
        builder.registrar_for(PluginKey("tests.ext")).register_block_type(
            _block_with_ref("tests.target")
        )
        builder.build(strata_version="0.1.0")

    with pytest.raises(UndeclaredPluginDependencyError):
        build(declare=False)
    build(declare=True)
