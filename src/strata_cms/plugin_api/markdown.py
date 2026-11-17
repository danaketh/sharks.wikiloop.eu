"""Pure Markdown contract: normalization, restricted-profile validation, preview.

Markdown is the persisted rich-text format. Validation is a pure function of the
source text (no database, network, or storage access). Persisted source is never
sanitized silently; disallowed constructs are rejected so editors can fix them.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from urllib.parse import unquote

from markdown_it import MarkdownIt
from markdown_it.token import Token

from strata_cms.plugin_api.schema import ValidationIssue


@dataclass(frozen=True, slots=True)
class MarkdownText:
    """Typed Markdown source owned by one rich-text field."""

    source: str


@dataclass(frozen=True, slots=True)
class MarkdownProfile:
    """Allow-list describing which Markdown constructs a field accepts."""

    allowed_heading_levels: frozenset[int]
    allowed_link_schemes: frozenset[str]
    allow_relative_links: bool
    allow_tables: bool
    max_length: int
    max_nesting_depth: int = 8


DEFAULT_MARKDOWN_PROFILE = MarkdownProfile(
    allowed_heading_levels=frozenset({2, 3, 4}),
    allowed_link_schemes=frozenset({"https", "http", "mailto"}),
    allow_relative_links=True,
    allow_tables=True,
    max_length=200_000,
    max_nesting_depth=8,
)

_BASE_TOKEN_TYPES = frozenset(
    {
        "paragraph_open",
        "paragraph_close",
        "heading_open",
        "heading_close",
        "bullet_list_open",
        "bullet_list_close",
        "ordered_list_open",
        "ordered_list_close",
        "list_item_open",
        "list_item_close",
        "blockquote_open",
        "blockquote_close",
        "fence",
        "code_block",
        "hr",
        "inline",
        "text",
        "softbreak",
        "hardbreak",
        "code_inline",
        "em_open",
        "em_close",
        "strong_open",
        "strong_close",
        "link_open",
        "link_close",
    }
)
_TABLE_TOKEN_TYPES = frozenset(
    {
        "table_open",
        "table_close",
        "thead_open",
        "thead_close",
        "tbody_open",
        "tbody_close",
        "tr_open",
        "tr_close",
        "th_open",
        "th_close",
        "td_open",
        "td_close",
    }
)
_CONTAINER_OPEN = frozenset(
    {"blockquote_open", "bullet_list_open", "ordered_list_open"}
)
_CONTAINER_CLOSE = frozenset(
    {"blockquote_close", "bullet_list_close", "ordered_list_close"}
)
_SCHEME = re.compile(r"^([^/?#]*?):")
_VALID_SCHEME_NAME = re.compile(r"^[a-z][a-z0-9+.-]*$")
_CONTROL_AND_SPACE = re.compile(r"[\x00-\x20\x7f-\x9f\u2028\u2029]")


def normalize_markdown(source: str) -> str:
    """Canonicalize line endings and document-end whitespace.

    Normalization never changes rendering: hard line breaks and spaces inside
    code blocks are preserved; only the end of the whole document is trimmed.
    """
    text = source.replace("\r\n", "\n").replace("\r", "\n").rstrip()
    return f"{text}\n" if text else ""


def validate_markdown(
    source: str,
    *,
    profile: MarkdownProfile = DEFAULT_MARKDOWN_PROFILE,
    path: str = "$",
) -> tuple[ValidationIssue, ...]:
    """Return one issue per distinct violation of the Markdown profile."""
    if len(source) > profile.max_length:
        return (
            ValidationIssue(
                code="markdown_too_long",
                message=f"Markdown must not exceed {profile.max_length} characters.",
                path=path,
            ),
        )
    issues: dict[tuple[str, str], ValidationIssue] = {}

    def report(code: str, message: str) -> None:
        issues.setdefault(
            (code, message), ValidationIssue(code=code, message=message, path=path)
        )

    allowed_types = _BASE_TOKEN_TYPES | (
        _TABLE_TOKEN_TYPES if profile.allow_tables else frozenset()
    )
    parser = _validation_parser(profile.max_nesting_depth)
    tokens = parser.parse(source)
    if _too_deep(tokens, profile.max_nesting_depth):
        report(
            "markdown_nesting_too_deep",
            f"Nesting deeper than {profile.max_nesting_depth} levels is not allowed.",
        )
    for token in _walk(tokens):
        _check_token(token, profile, allowed_types, report)
    return tuple(issues.values())


def render_markdown_preview(source: str) -> str:
    """Render HTML for admin preview only; raw HTML in the source is escaped."""
    html: str = _PREVIEW_PARSER.render(source)
    return html


class _ValidationParser(MarkdownIt):
    """Parser that keeps unsafe links as tokens so they can be reported."""

    def validateLink(self, url: str) -> bool:  # noqa: N802 - library hook name
        """Accept every destination; the profile check judges it afterwards."""
        del url
        return True


def _parser_nesting_limit(max_nesting_depth: int) -> int:
    """Return markdown-it's ``maxNesting`` for one profile depth cap."""
    return 4 * max_nesting_depth + 16


def _inline_bracket_limit(max_nesting_depth: int) -> int:
    """Return the deepest link-label bracket nesting validation accepts.

    markdown-it's inline recursion level grows by one per still-open ``[``
    while it scans a link label (unmatched brackets included), and past
    ``maxNesting`` it silently stops recognizing links. Half the parser limit
    keeps a wide margin, so accepted source is never truncated.
    """
    return _parser_nesting_limit(max_nesting_depth) // 2


@cache
def _validation_parser(max_nesting_depth: int) -> MarkdownIt:
    # markdown-it silently drops everything past ``maxNesting``. The limit sits
    # far above what the depth checks allow (a container costs at most two
    # levels; inline brackets are capped at half the limit), so any source deep
    # enough to be truncated has already tripped the nesting check and cannot
    # hide content from validation.
    limit = _parser_nesting_limit(max_nesting_depth)
    return _ValidationParser("commonmark", {"html": True, "maxNesting": limit}).enable(
        "table"
    )


_PREVIEW_PARSER = MarkdownIt("commonmark", {"html": False}).enable("table")


def _too_deep(tokens: list[Token], max_depth: int) -> bool:
    """Detect container nesting beyond the profile cap or near-limit labels."""
    containers = 0
    bracket_limit = _inline_bracket_limit(max_depth)
    for token in tokens:
        if token.type in _CONTAINER_OPEN:
            containers += 1
        elif token.type in _CONTAINER_CLOSE:
            containers -= 1
        if containers > max_depth:
            return True
        if token.type == "inline" and _bracket_depth(token.content) >= bracket_limit:
            return True
    return False


def _bracket_depth(content: str) -> int:
    """Return the deepest still-open ``[`` count within one inline token.

    Mirrors how markdown-it recurses while scanning link labels: backslash
    escapes and closed code spans are skipped (the parser consumes them whole),
    unmatched ``[`` keep counting. Anything uncertain over-counts, which only
    rejects more input, never less.
    """
    depth = deepest = 0
    index = 0
    length = len(content)
    while index < length:
        char = content[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            index = _skip_code_span(content, index)
            continue
        if char == "[":
            depth += 1
            deepest = max(deepest, depth)
        elif char == "]" and depth:
            depth -= 1
        index += 1
    return deepest


def _skip_code_span(content: str, start: int) -> int:
    """Return the index after a code span opened at ``start``, or its run."""
    run_end = start
    while run_end < len(content) and content[run_end] == "`":
        run_end += 1
    run = run_end - start
    search = run_end
    while (found := content.find("`" * run, search)) != -1:
        closer_end = found + run
        if closer_end < len(content) and content[closer_end] == "`":
            while closer_end < len(content) and content[closer_end] == "`":
                closer_end += 1
            search = closer_end
            continue
        return closer_end
    return run_end


def _walk(tokens: list[Token]) -> list[Token]:
    flat: list[Token] = []
    for token in tokens:
        flat.append(token)
        if token.children:
            flat.extend(_walk(token.children))
    return flat


def _check_token(
    token: Token,
    profile: MarkdownProfile,
    allowed_types: frozenset[str],
    report: Callable[[str, str], None],
) -> None:
    kind = token.type
    if kind in {"html_block", "html_inline"}:
        report("markdown_html_not_allowed", "Raw HTML is not allowed.")
    elif kind == "image":
        report("markdown_image_not_allowed", "Images are not allowed.")
    elif kind == "heading_open":
        level = int(token.tag[1:])
        if level not in profile.allowed_heading_levels:
            report(
                "markdown_heading_level_not_allowed",
                f"Heading level {level} is not allowed.",
            )
    elif kind == "link_open":
        href = str(token.attrGet("href") or "")
        if not _link_allowed(href, profile):
            report(
                "markdown_link_scheme_not_allowed",
                "Link destination is not allowed.",
            )
    elif kind not in allowed_types:
        report(
            "markdown_construct_not_allowed",
            f"Markdown construct '{kind}' is not allowed.",
        )


def _link_allowed(href: str, profile: MarkdownProfile) -> bool:
    """Judge a destination as a browser would after decoding and stripping."""
    candidate = _CONTROL_AND_SPACE.sub("", unquote(href)).lower().replace("\\", "/")
    match = _SCHEME.match(candidate)
    if match is not None:
        scheme = match.group(1)
        return bool(
            _VALID_SCHEME_NAME.fullmatch(scheme)
            and scheme in profile.allowed_link_schemes
        )
    if candidate.startswith("//"):
        return False
    return profile.allow_relative_links
