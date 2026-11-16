"""The ``strata.rich_text`` block: one restricted Markdown document."""

from strata_cms.domain.revision import RevisionData
from strata_cms.plugin_api import (
    BlockDefinition,
    BlockEditorDefinition,
    BlockSchema,
    BlockTypeKey,
    EditorInputKind,
    MarkdownText,
    PluginDefinition,
    PluginKey,
    ScalarEditorField,
    SchemaDecodeError,
    ValidationIssue,
    normalize_markdown,
    validate_markdown,
)

RICH_TEXT_PLUGIN_KEY = PluginKey("strata.rich_text")
RICH_TEXT_BLOCK_TYPE = BlockTypeKey("strata.rich_text")

RICH_TEXT_PLUGIN = PluginDefinition(
    key=RICH_TEXT_PLUGIN_KEY,
    version="1.0.0",
    requires_strata=">=0.1,<0.2",
)


class RichTextBlockSchema(BlockSchema[MarkdownText]):
    """Block data is ``{"text": "<markdown>"}`` normalized on every write."""

    value_type = MarkdownText

    def deserialize(self, data: RevisionData) -> MarkdownText:
        """Decode and canonicalize the Markdown source."""
        text = data.as_dict().get("text")
        if not isinstance(text, str):
            raise SchemaDecodeError("Rich text block requires a string text field.")
        return MarkdownText(source=normalize_markdown(text))

    def serialize(self, value: MarkdownText) -> RevisionData:
        """Encode canonical block data."""
        return RevisionData.from_mapping({"text": normalize_markdown(value.source)})

    def validate(self, value: MarkdownText) -> tuple[ValidationIssue, ...]:
        """Enforce the default Markdown profile."""
        return validate_markdown(value.source, path="$.text")


RICH_TEXT_BLOCK = BlockDefinition(
    key=RICH_TEXT_BLOCK_TYPE,
    schema_version=1,
    schema=RichTextBlockSchema(),
    editor=BlockEditorDefinition(
        label="Rich text",
        description="Formatted text written in restricted Markdown.",
        icon="notes",
        initial_data=RevisionData.from_mapping({"text": ""}),
        fields=(
            ScalarEditorField(
                path=("text",),
                label="Text",
                input_kind=EditorInputKind.MARKDOWN,
                required=False,
            ),
        ),
    ),
)
