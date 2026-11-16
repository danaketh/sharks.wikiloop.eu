from dataclasses import replace

from strata_cms.domain.revision import RevisionData
from strata_cms.examples.simple_article import (
    EXAMPLE_ARTICLE,
    EXAMPLE_PLUGIN,
    ArticleData,
)
from strata_cms.examples.structured_page import (
    LANDING_PAGE,
    LANDING_PAGE_TYPE,
    SECTION_BLOCK,
    TEXT_BLOCK,
    TextBlockData,
)
from strata_cms.plugin_api import (
    StrataRegistry,
    StrataRegistryBuilder,
    ValidationIssue,
)


class NonBlankBody:
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


class TextMustSayReady:
    def publish_issues(self, value: TextBlockData) -> tuple[ValidationIssue, ...]:
        if value.text == "ready":
            return ()
        return (
            ValidationIssue(
                code="text_not_ready",
                message="Text must be ready.",
                path="$.text",
            ),
        )


def _registry() -> StrataRegistry:
    builder = StrataRegistryBuilder()
    builder.register_plugin(EXAMPLE_PLUGIN)
    registrar = builder.registrar_for(EXAMPLE_PLUGIN.key)
    registrar.register_block_type(replace(TEXT_BLOCK, readiness=TextMustSayReady()))
    registrar.register_block_type(SECTION_BLOCK)
    registrar.register_content_type(EXAMPLE_ARTICLE)
    registrar.register_content_type(LANDING_PAGE)
    return builder.build(strata_version="0.1.0")


def _text(block_id: int, text: str) -> dict[str, object]:
    return {
        "id": f"00000000-0000-0000-0000-{block_id:012d}",
        "type": "strata_examples.text",
        "version": 2,
        "data": {"text": text},
        "slots": {},
    }


def test_definition_without_readiness_reports_no_issues() -> None:
    value = ArticleData(title="T", body="")

    assert EXAMPLE_ARTICLE.publish_issues_untyped(value) == ()
    assert TEXT_BLOCK.publish_issues_untyped(TextBlockData(text="")) == ()


def test_definition_readiness_issues_are_returned_unchanged() -> None:
    definition = replace(EXAMPLE_ARTICLE, readiness=NonBlankBody())

    issues = definition.publish_issues_untyped(ArticleData(title="T", body=" "))

    assert [issue.code for issue in issues] == ["body_required_to_publish"]
    assert issues[0].path == "$.body"
    assert definition.publish_issues_untyped(ArticleData(title="T", body="x")) == ()


def test_block_registry_collects_nested_block_readiness_with_prefixed_paths() -> None:
    section = {
        "id": "00000000-0000-0000-0000-000000000900",
        "type": "strata_examples.section",
        "version": 1,
        "data": {"variant": "default"},
        "slots": {"children": [_text(1, "ready"), _text(2, "draft")]},
    }
    data = RevisionData.from_mapping(
        {"title": "T", "body": [_text(3, "draft"), section]}
    )
    registry = _registry()
    fields = registry.content_types.require(LANDING_PAGE_TYPE).definition.block_fields

    issues = registry.blocks.publish_issues(data, fields)

    assert [issue.path for issue in issues] == [
        "$.body[0].data.text",
        "$.body[1].slots.children[1].data.text",
    ]
    assert {issue.code for issue in issues} == {"text_not_ready"}
