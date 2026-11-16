"""Pure Markdown contract: normalization, restricted-profile validation, preview.

Markdown is the persisted rich-text format. Validation is a pure function of the
source text (no database, network, or storage access). Persisted source is never
sanitized silently; disallowed constructs are rejected so editors can fix them.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
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


DEFAULT_MARKDOWN_PROFILE = MarkdownProfile(
    allowed_heading_levels=frozenset({2, 3, 4}),
    allowed_link_schemes=frozenset({"https", "http", "mailto"}),
    allow_relative_links=True,
    allow_tables=True,
    max_length=200_000,
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
_SCHEME = re.compile(r"^([^/?#]*?):")
_VALID_SCHEME_NAME = re.compile(r"^[a-z][a-z0-9+.-]*$")
_CONTROL_AND_SPACE = re.compile(r"[\x00-\x20\x7f-\x9f\u2028\u2029]")


def normalize_markdown(source: str) -> str:
    """Canonicalize line endings and trailing whitespace without reflowing text."""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    text = "\n".join(line.rstrip() for line in lines).rstrip("\n")
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
    for token in _walk(_validation_parser().parse(source)):
        _check_token(token, profile, allowed_types, report)
    return tuple(issues.values())


def render_markdown_preview(source: str) -> str:
    """Render HTML for admin preview only; raw HTML in the source is escaped."""
    html: str = _preview_parser().render(source)
    return html


def _validation_parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": True}).enable("table")
    # Keep unsafe links as link tokens so their destinations can be reported
    # instead of being silently downgraded to text by the library.
    parser.validateLink = _accept_any_link  # type: ignore[method-assign]
    return parser


def _accept_any_link(url: str) -> bool:
    del url
    return True


def _preview_parser() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": False}).enable("table")


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
