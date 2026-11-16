import pytest

from strata_cms.contrib.rich_text.apps import RichTextPluginConfig
from strata_cms.contrib.rich_text.blocks import (
    RICH_TEXT_BLOCK,
    RICH_TEXT_BLOCK_TYPE,
    RICH_TEXT_PLUGIN,
)
from strata_cms.plugin_api import (
    BlockCollection,
    BlockValidationError,
    EditorInputKind,
    StrataRegistry,
    StrataRegistryBuilder,
)

_ID = "00000000-0000-0000-0000-000000000301"


def _registry() -> StrataRegistry:
    builder = StrataRegistryBuilder()
    builder.register_plugin(RICH_TEXT_PLUGIN)
    registrar = builder.registrar_for(RICH_TEXT_PLUGIN.key)
    registrar.register_block_type(RICH_TEXT_BLOCK)
    return builder.build(strata_version="0.1.0")


def _collection(text: str) -> BlockCollection:
    return BlockCollection.from_json(
        [
            {
                "id": _ID,
                "type": str(RICH_TEXT_BLOCK_TYPE),
                "version": 1,
                "data": {"text": text},
                "slots": {},
            }
        ]
    )


def test_block_key_and_plugin_key() -> None:
    assert str(RICH_TEXT_BLOCK_TYPE) == "strata.rich_text"
    assert str(RICH_TEXT_PLUGIN.key) == "strata.rich_text"
    assert RichTextPluginConfig.strata_plugin is RICH_TEXT_PLUGIN


def test_rich_text_block_normalizes_text_on_write() -> None:
    normalized = _registry().blocks.normalize_collection(_collection("a  \r\n"))

    assert normalized.blocks[0].data.as_dict() == {"text": "a\n"}


def test_rich_text_block_rejects_html() -> None:
    with pytest.raises(BlockValidationError) as raised:
        _registry().blocks.normalize_collection(_collection("<b>x</b>\n"))

    issue = raised.value.issues[0]
    assert issue.code == "markdown_html_not_allowed"
    assert issue.path == "$[0].data.text"


def test_rich_text_block_registered_with_markdown_editor() -> None:
    editor = _registry().blocks.require(RICH_TEXT_BLOCK_TYPE).definition.editor

    assert editor is not None
    (field,) = editor.fields
    assert field.path == ("text",)
    assert field.input_kind == EditorInputKind.MARKDOWN


def test_rich_text_delivery_exposes_markdown_source() -> None:
    rendered = _registry().blocks.delivery_collection(_collection("## Hi\n"))

    first = rendered[0]
    assert isinstance(first, dict)
    assert first["data"] == {"text": "## Hi\n"}
