from dataclasses import replace

import pytest

from strata_cms.application.editor_models import ScalarEditorFieldSpec
from strata_cms.domain.revision import RevisionData
from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.examples.simple_article import (
    EXAMPLE_ARTICLE,
    EXAMPLE_CONTENT_TYPE,
    register_example,
)
from strata_cms.examples.structured_page import LANDING_PAGE_TYPE
from strata_cms.infrastructure.editors.registry import RegistryEditorCatalog
from strata_cms.plugin_api import (
    BlockCollectionEditorField,
    BlockEditorDefinition,
    ContentEditorDefinition,
    ContentTypeDefinition,
    EditorChoice,
    EditorInputKind,
    LinkPath,
    PluginDefinition,
    PluginKey,
    ReferenceFieldDefinition,
    ScalarEditorField,
    StrataRegistryBuilder,
    ValidationIssue,
)
from strata_cms.plugin_api.errors import RegistryConfigurationError


def _catalog() -> RegistryEditorCatalog:
    builder = StrataRegistryBuilder()
    register_example(builder)
    return RegistryEditorCatalog(builder.build(strata_version="0.1.0"))


def test_catalog_lists_human_editable_content_types() -> None:
    items = _catalog().list_content_types()

    assert [(item.key, item.label, item.editable) for item in items] == [
        ("strata_examples.article", "Article", True),
        ("strata_examples.landing_page", "Landing page", True),
    ]


def test_article_editor_is_independent_from_schema_validation() -> None:
    editor = _catalog().get_content_type(EXAMPLE_CONTENT_TYPE)

    assert editor is not None
    assert editor.schema_version == EXAMPLE_ARTICLE.schema_version
    assert editor.initial_data == {"body": "", "title": ""}
    assert [field.label for field in editor.fields] == ["Title", "Body"]


def test_landing_page_editor_includes_only_reachable_blocks() -> None:
    editor = _catalog().get_content_type(LANDING_PAGE_TYPE)

    assert editor is not None
    assert [block.key for block in editor.blocks] == [
        "strata_examples.section",
        "strata_examples.text",
    ]
    section = editor.blocks[0]
    assert section.slots[0].name == "children"
    assert section.slots[0].constraint.allowed_types == ("strata_examples.text",)


def test_block_templates_do_not_allocate_persistent_identity() -> None:
    editor = _catalog().get_content_type(LANDING_PAGE_TYPE)

    assert editor is not None
    text = next(block for block in editor.blocks if block.key.endswith(".text"))
    assert text.initial_data == {"text": ""}
    assert "id" not in text.initial_data


def test_choice_fields_require_explicit_options() -> None:
    with pytest.raises(RegistryConfigurationError):
        ScalarEditorField(
            path=("variant",),
            label="Variant",
            input_kind=EditorInputKind.CHOICE,
        )


def test_choice_values_are_unique() -> None:
    with pytest.raises(RegistryConfigurationError):
        ScalarEditorField(
            path=("variant",),
            label="Variant",
            input_kind=EditorInputKind.CHOICE,
            choices=(
                EditorChoice("same", "A"),
                EditorChoice("same", "B"),
            ),
        )


def test_content_editor_cannot_duplicate_paths() -> None:
    field = ScalarEditorField(path=("title",), label="Title")

    with pytest.raises(RegistryConfigurationError):
        ContentEditorDefinition(label="Example", fields=(field, field))


def test_block_editor_initial_data_is_immutable_revision_data() -> None:
    initial = {"text": "value"}
    editor = BlockEditorDefinition(
        label="Text",
        initial_data=RevisionData.from_mapping(initial),
    )
    initial["text"] = "changed"

    assert editor.initial_data.as_dict() == {"text": "value"}


def test_content_type_editor_rejects_block_ui_for_non_block_field() -> None:
    editor = ContentEditorDefinition(
        label="Bad article",
        fields=(BlockCollectionEditorField(path=("body",), label="Body"),),
    )

    with pytest.raises(RegistryConfigurationError):
        replace(EXAMPLE_ARTICLE, editor=editor)


def test_unknown_content_type_has_no_editor_contract() -> None:
    editor = _catalog().get_content_type(ContentTypeKey("tests.missing"))

    assert editor is None


def test_editor_rejects_ancestor_descendant_field_overlap() -> None:
    with pytest.raises(RegistryConfigurationError):
        ContentEditorDefinition(
            label="Overlapping",
            fields=(
                ScalarEditorField(path=("metadata",), label="Metadata"),
                ScalarEditorField(
                    path=("metadata", "title"),
                    label="Metadata title",
                ),
            ),
        )


class _DictSchema:
    value_type: type[dict[str, object]] = dict

    def deserialize(self, data: RevisionData) -> dict[str, object]:
        return data.as_dict()

    def serialize(self, value: dict[str, object]) -> RevisionData:
        return RevisionData.from_mapping(value)

    def validate(self, value: dict[str, object]) -> tuple[ValidationIssue, ...]:
        del value
        return ()


def _reference_catalog(
    *, editor_types: tuple[ContentTypeKey, ...], many: bool = False
) -> RegistryEditorCatalog:
    species = ContentTypeKey("tests.species")
    profile = ContentTypeKey("tests.profile")
    builder = StrataRegistryBuilder()
    plugin = PluginDefinition(
        key=PluginKey("tests.editor_refs"),
        version="1.0.0",
        requires_strata=">=0.1,<0.2",
    )
    builder.register_plugin(plugin)
    registrar = builder.registrar_for(plugin.key)
    registrar.register_content_type(
        ContentTypeDefinition(
            key=species,
            schema_version=1,
            schema=_DictSchema(),
            summary_title=lambda value: str(value.get("title", "")),
        )
    )
    registrar.register_content_type(
        ContentTypeDefinition(
            key=profile,
            schema_version=1,
            schema=_DictSchema(),
            reference_fields=(
                ReferenceFieldDefinition(
                    LinkPath.parse("species"), frozenset({species}), many=many
                ),
            ),
            editor=ContentEditorDefinition(
                label="Profile",
                fields=(
                    ScalarEditorField(
                        path=("species",),
                        label="Species",
                        input_kind=EditorInputKind.REFERENCE,
                        reference_types=editor_types,
                    ),
                    ScalarEditorField(
                        path=("bio",), label="Bio", input_kind=EditorInputKind.MARKDOWN
                    ),
                ),
            ),
        )
    )
    return RegistryEditorCatalog(builder.build(strata_version="0.1.0"))


def test_editor_contract_exposes_reference_types() -> None:
    editor = _reference_catalog(
        editor_types=(ContentTypeKey("tests.species"),)
    ).get_content_type(ContentTypeKey("tests.profile"))

    assert editor is not None
    by_path = {field.path: field for field in editor.fields}
    species = by_path[("species",)]
    assert isinstance(species, ScalarEditorFieldSpec)
    assert species.input_kind == "reference"
    assert species.reference_types == ("tests.species",)
    assert species.reference_many is False
    bio = by_path[("bio",)]
    assert isinstance(bio, ScalarEditorFieldSpec)
    assert bio.input_kind == "markdown"
    assert bio.reference_types == ()
    assert bio.reference_many is False


def test_editor_reference_types_follow_the_reference_field_definition() -> None:
    editor = _reference_catalog(
        editor_types=(ContentTypeKey("tests.other"),)
    ).get_content_type(ContentTypeKey("tests.profile"))

    assert editor is not None
    species = next(f for f in editor.fields if f.path == ("species",))
    assert isinstance(species, ScalarEditorFieldSpec)
    assert species.reference_types == ("tests.species",)


def test_editor_contract_marks_many_reference_fields() -> None:
    editor = _reference_catalog(
        editor_types=(ContentTypeKey("tests.species"),), many=True
    ).get_content_type(ContentTypeKey("tests.profile"))

    assert editor is not None
    species = next(f for f in editor.fields if f.path == ("species",))
    assert isinstance(species, ScalarEditorFieldSpec)
    assert species.reference_many is True
