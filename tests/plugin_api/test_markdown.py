from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from strata_cms.plugin_api import (
    DEFAULT_MARKDOWN_PROFILE,
    MarkdownText,
    normalize_markdown,
    render_markdown_preview,
    validate_markdown,
)


@pytest.mark.parametrize(
    "src",
    [
        "## Hi\n",
        "### Hi\n",
        "#### Hi\n",
        "*a* **b** `c`\n",
        "- x\n- y\n",
        "1. x\n2. y\n",
        "> q\n",
        "| a | b |\n|---|---|\n| 1 | 2 |\n",
        "[x](https://e.org)\n",
        "[x](http://e.org)\n",
        "[x](/species/mako)\n",
        "[x](#anchor)\n",
        "[m](mailto:a@b.cz)\n",
        "```\ncode\n```\n",
        "---\n",
        "",
    ],
)
def test_allowed_constructs_have_no_issues(src: str) -> None:
    assert validate_markdown(src) == ()


@pytest.mark.parametrize(
    ("src", "code"),
    [
        ("<b>x</b>\n", "markdown_html_not_allowed"),
        ("x <span>y</span>\n", "markdown_html_not_allowed"),
        ("![a](https://e.org/a.png)\n", "markdown_image_not_allowed"),
        ("# H1\n", "markdown_heading_level_not_allowed"),
        ("##### H5\n", "markdown_heading_level_not_allowed"),
        ("###### H6\n", "markdown_heading_level_not_allowed"),
        ("[x](javascript:alert(1))\n", "markdown_link_scheme_not_allowed"),
        ("[x](JaVaScRiPt:alert(1))\n", "markdown_link_scheme_not_allowed"),
        ("[x](&#106;avascript:alert(1))\n", "markdown_link_scheme_not_allowed"),
        ("[x](java&#9;script:alert(1))\n", "markdown_link_scheme_not_allowed"),
        ("[x](data:text/html,hi)\n", "markdown_link_scheme_not_allowed"),
        ("[x](ftp://e.org/f)\n", "markdown_link_scheme_not_allowed"),
        ("<javascript:alert(1)>\n", "markdown_link_scheme_not_allowed"),
        ("[x][r]\n\n[r]: javascript:alert(1)\n", "markdown_link_scheme_not_allowed"),
        ("[x](//evil.example/a)\n", "markdown_link_scheme_not_allowed"),
    ],
)
def test_disallowed_constructs(src: str, code: str) -> None:
    assert code in {i.code for i in validate_markdown(src)}


def test_profile_defaults() -> None:
    assert DEFAULT_MARKDOWN_PROFILE.allowed_heading_levels == frozenset({2, 3, 4})
    assert DEFAULT_MARKDOWN_PROFILE.allowed_link_schemes == frozenset(
        {"https", "http", "mailto"}
    )
    assert DEFAULT_MARKDOWN_PROFILE.allow_relative_links is True
    assert DEFAULT_MARKDOWN_PROFILE.allow_tables is True
    assert DEFAULT_MARKDOWN_PROFILE.max_length == 200_000
    assert MarkdownText("x").source == "x"


def test_too_long_is_rejected() -> None:
    issues = validate_markdown("a" * 200_001)

    assert [i.code for i in issues] == ["markdown_too_long"]


def test_max_length_boundary_is_allowed() -> None:
    assert validate_markdown("a" * 200_000) == ()


def test_issue_paths_use_given_prefix() -> None:
    issues = validate_markdown("<b>x</b>\n", path="$.body")

    assert issues
    assert {i.path for i in issues} == {"$.body"}


def test_nested_disallowed_constructs_are_found() -> None:
    issues = validate_markdown("> - [x](javascript:alert(1))\n")

    assert "markdown_link_scheme_not_allowed" in {i.code for i in issues}


@given(st.text())
def test_normalize_is_idempotent(s: str) -> None:
    assert normalize_markdown(normalize_markdown(s)) == normalize_markdown(s)


def test_normalize_does_not_reflow() -> None:
    assert normalize_markdown("a\nb  \r\n") == "a\nb\n"


def test_normalize_empty_stays_empty() -> None:
    assert normalize_markdown("") == ""


def test_normalize_collapses_trailing_newlines_and_cr() -> None:
    assert normalize_markdown("a\rb\n\n\n") == "a\nb\n"


@given(st.text())
def test_preview_never_emits_raw_html_tags_from_input(s: str) -> None:
    assert "<script" not in render_markdown_preview(s).lower()


def test_preview_renders_tables_and_escapes_html() -> None:
    html = render_markdown_preview("| a |\n|---|\n| 1 |\n\n<b>x</b>\n")

    assert "<table>" in html
    assert "<b>" not in html


def test_tables_rejected_when_profile_disallows_them() -> None:
    profile = replace(DEFAULT_MARKDOWN_PROFILE, allow_tables=False)

    issues = validate_markdown("| a |\n|---|\n| 1 |\n", profile=profile)

    assert "markdown_construct_not_allowed" in {i.code for i in issues}


def test_relative_links_rejected_when_profile_disallows_them() -> None:
    profile = replace(DEFAULT_MARKDOWN_PROFILE, allow_relative_links=False)

    issues = validate_markdown("[x](/a)\n", profile=profile)

    assert "markdown_link_scheme_not_allowed" in {i.code for i in issues}


@pytest.mark.parametrize("depth", [9, 15, 19, 20, 25, 100])
@pytest.mark.parametrize(
    "payload",
    ["<b>x</b>", "<script>alert(1)</script>", "[x](javascript:alert(1))"],
)
def test_deeply_nested_blockquotes_are_never_unvalidated(
    depth: int, payload: str
) -> None:
    codes = {i.code for i in validate_markdown("> " * depth + payload + "\n")}

    assert "markdown_nesting_too_deep" in codes


@pytest.mark.parametrize("depth", [9, 10, 25, 100])
def test_deeply_nested_lists_are_never_unvalidated(depth: int) -> None:
    lines = [f"{'  ' * i}- item" for i in range(depth)]
    src = "\n".join([*lines, f"{'  ' * depth}- [x](javascript:alert(1))"]) + "\n"

    assert "markdown_nesting_too_deep" in {i.code for i in validate_markdown(src)}


def test_deep_link_label_nesting_is_reported() -> None:
    src = "[" * 60 + "[x](javascript:alert(1))" + "]" * 60 + "\n"

    assert "markdown_nesting_too_deep" in {i.code for i in validate_markdown(src)}


def test_interval_notation_across_paragraphs_is_allowed() -> None:
    src = "\n\n".join(f"Range [0, {n}) applies." for n in range(12)) + "\n"

    assert validate_markdown(src) == ()


def test_interval_notation_within_one_paragraph_is_allowed() -> None:
    src = " ".join(f"[0, {n})" for n in range(12)) + "\n"

    assert validate_markdown(src) == ()


@pytest.mark.parametrize(
    "src",
    [
        "```\n" + "[" * 30 + "\n```\n",
        "    " + "[" * 30 + "\n",
        "Use `" + "[" * 30 + "` here.\n",
        "Use ``a ` " + "[" * 30 + "`` here.\n",
        "Escaped " + "\\[" * 30 + " text.\n",
    ],
)
def test_brackets_in_code_or_escaped_do_not_count_as_nesting(src: str) -> None:
    assert validate_markdown(src) == ()


@pytest.mark.parametrize(
    "src",
    [
        "[" * 30 + "[x](javascript:alert(1))\n",
        "[" * 47 + "[x](javascript:alert(1))\n",
        "`unclosed " + "[" * 30 + "[x](javascript:alert(1))\n",
        "\\\\" + "[" * 30 + "[x](javascript:alert(1))\n",
        "> " + "[" * 60 + "x\n",
    ],
)
def test_bracket_nesting_near_parser_limit_is_reported(src: str) -> None:
    assert "markdown_nesting_too_deep" in {i.code for i in validate_markdown(src)}


def test_reasonable_nesting_is_allowed() -> None:
    lines = [f"{'  ' * i}- item {i}" for i in range(4)]
    src = "\n".join("> " + line for line in lines) + "\n\n> *a **b *c* d** e*\n"

    assert validate_markdown(src) == ()


def test_normalize_preserves_hard_line_breaks() -> None:
    assert normalize_markdown("a  \nb\n") == "a  \nb\n"


def test_normalize_preserves_trailing_spaces_in_code_fences() -> None:
    src = "```\nx  \ny\n```\n"

    assert normalize_markdown(src) == src
