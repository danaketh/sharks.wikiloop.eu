"""The ``strata.media`` content type: one uploaded blob plus editorial metadata.

Write validation covers structure, the required ``file`` blob link, https-only
attribution URLs and the restricted Markdown caption. Alt text and attribution
completeness are publish-readiness rules, so a draft can be saved right after
an upload. The blob's URL and technical metadata are resolved by core at
Delivery time from the declared blob field.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from strata_cms.domain.revision import RevisionData
from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.plugin_api import (
    BlobFieldDefinition,
    BlobReference,
    ContentCapability,
    ContentEditorDefinition,
    ContentSchema,
    ContentTypeDefinition,
    ContentValidationError,
    EditorInputKind,
    LinkPath,
    MarkdownText,
    PluginDefinition,
    PluginKey,
    PublishReadiness,
    ScalarEditorField,
    SchemaDecodeError,
    ValidationIssue,
    normalize_markdown,
    validate_markdown,
)

MEDIA_PLUGIN_KEY = PluginKey("strata.media")
MEDIA_CONTENT_TYPE = ContentTypeKey("strata.media")
UNTITLED_MEDIA = "Untitled media"
_MAX_URL_LENGTH = 2048
_ATTRIBUTION_FIELDS = ("author", "license", "license_url", "source_url", "credit_line")
_REQUIRED_ATTRIBUTION = ("author", "license", "source_url")
_HTTPS_ATTRIBUTION = ("license_url", "source_url")

MEDIA_PLUGIN = PluginDefinition(
    key=MEDIA_PLUGIN_KEY,
    version="1.0.0",
    requires_strata=">=0.1,<0.2",
)


@dataclass(frozen=True, slots=True)
class Attribution:
    """Credit metadata; blank values are normalized to ``None`` (unset)."""

    author: str | None = None
    license: str | None = None
    license_url: str | None = None
    source_url: str | None = None
    credit_line: str | None = None


@dataclass(frozen=True, slots=True)
class MediaData:
    """Typed current-version (v1) representation of a media item."""

    file: BlobReference
    title: str
    alt_text: str
    decorative: bool
    caption: MarkdownText | None
    attribution: Attribution
    requires_attribution: bool


def _string(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise SchemaDecodeError(f"Media field '{key}' must be a string.")
    return value


def _boolean(raw: Mapping[str, object], key: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise SchemaDecodeError(f"Media field '{key}' must be a boolean.")
    return value


def _optional_text(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaDecodeError(f"Attribution field '{key}' must be a string.")
    stripped = value.strip()
    return stripped or None


def _decode_file(raw: Mapping[str, object]) -> BlobReference:
    value = raw.get("file")
    if value is None:
        raise ContentValidationError(
            (
                ValidationIssue(
                    code="file_required",
                    message="Media requires an uploaded file.",
                    path="$.file",
                ),
            )
        )
    return BlobReference.from_json(value)


def _decode_caption(raw: Mapping[str, object]) -> MarkdownText | None:
    value = raw.get("caption")
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaDecodeError("Media caption must be a Markdown string or null.")
    source = normalize_markdown(value)
    return MarkdownText(source=source) if source else None


def _decode_attribution(raw: Mapping[str, object]) -> Attribution:
    value = raw.get("attribution")
    if value is None:
        return Attribution()
    if not isinstance(value, Mapping):
        raise SchemaDecodeError("Media attribution must be an object.")
    unknown = set(value) - set(_ATTRIBUTION_FIELDS)
    if unknown:
        raise SchemaDecodeError(
            f"Unknown attribution field(s): {', '.join(sorted(unknown))}."
        )
    return Attribution(
        **{field: _optional_text(value, field) for field in _ATTRIBUTION_FIELDS}
    )


def is_https_url(value: str) -> bool:
    """Return whether ``value`` is an absolute ``https`` URL with a host."""
    if len(value) > _MAX_URL_LENGTH or any(
        char.isspace() or not char.isprintable() for char in value
    ):
        return False
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "https" and bool(hostname) and "@" not in parts.netloc


class MediaSchema(ContentSchema[MediaData]):
    """Canonical v1 JSON shape; optional fields default when absent."""

    value_type = MediaData

    def deserialize(self, data: RevisionData) -> MediaData:
        """Decode JSON, rejecting wrong types and a missing ``file`` link."""
        raw = data.as_dict()
        return MediaData(
            file=_decode_file(raw),
            title=_string(raw, "title"),
            alt_text=_string(raw, "alt_text"),
            decorative=_boolean(raw, "decorative"),
            caption=_decode_caption(raw),
            attribution=_decode_attribution(raw),
            requires_attribution=_boolean(raw, "requires_attribution"),
        )

    def serialize(self, value: MediaData) -> RevisionData:
        """Encode the full canonical shape, including unset (null) fields."""
        attribution = value.attribution
        return RevisionData.from_mapping(
            {
                "file": value.file.to_json(),
                "title": value.title,
                "alt_text": value.alt_text,
                "decorative": value.decorative,
                "caption": (
                    normalize_markdown(value.caption.source)
                    if value.caption is not None
                    else None
                ),
                "attribution": {
                    field: getattr(attribution, field) for field in _ATTRIBUTION_FIELDS
                },
                "requires_attribution": value.requires_attribution,
            }
        )

    def validate(self, value: MediaData) -> tuple[ValidationIssue, ...]:
        """Require https attribution URLs and a valid Markdown caption."""
        issues: list[ValidationIssue] = []
        for field in _HTTPS_ATTRIBUTION:
            url = getattr(value.attribution, field)
            if url is not None and not is_https_url(url):
                issues.append(
                    ValidationIssue(
                        code="attribution_url_not_https",
                        message="Attribution URLs must be absolute https URLs.",
                        path=f"$.attribution.{field}",
                    )
                )
        if value.caption is not None:
            issues.extend(validate_markdown(value.caption.source, path="$.caption"))
        return tuple(issues)


class MediaReadiness(PublishReadiness[MediaData]):
    """Alt text unless decorative; complete attribution when required."""

    def publish_issues(self, value: MediaData) -> tuple[ValidationIssue, ...]:
        """Return publish-blocking problems for one media value."""
        issues: list[ValidationIssue] = []
        if not value.decorative and not value.alt_text.strip():
            issues.append(
                ValidationIssue(
                    code="alt_text_required",
                    message="Non-decorative media needs alt text before publishing.",
                    path="$.alt_text",
                )
            )
        if value.requires_attribution:
            issues.extend(
                ValidationIssue(
                    code="attribution_incomplete",
                    message=f"Attribution {field} is required before publishing.",
                    path=f"$.attribution.{field}",
                )
                for field in _REQUIRED_ATTRIBUTION
                if getattr(value.attribution, field) is None
            )
        return tuple(issues)


def media_summary_title(value: MediaData) -> str:
    """Return the title, else the alt text, else a neutral placeholder."""
    return value.title.strip() or value.alt_text.strip() or UNTITLED_MEDIA


def _field(
    path: tuple[str, ...],
    label: str,
    input_kind: EditorInputKind = EditorInputKind.TEXT,
    help_text: str = "",
) -> ScalarEditorField:
    return ScalarEditorField(
        path=path,
        label=label,
        input_kind=input_kind,
        required=False,
        help_text=help_text,
    )


MEDIA_EDITOR = ContentEditorDefinition(
    label="Media",
    description="An uploaded image with alt text, caption and attribution.",
    icon="image",
    initial_data=RevisionData.from_mapping(
        {
            "file": None,
            "title": "",
            "alt_text": "",
            "decorative": False,
            "caption": None,
            "attribution": dict.fromkeys(_ATTRIBUTION_FIELDS),
            "requires_attribution": False,
        }
    ),
    fields=(
        ScalarEditorField(
            path=("file",), label="File", input_kind=EditorInputKind.BLOB
        ),
        _field(("title",), "Title"),
        _field(
            ("alt_text",),
            "Alt text",
            help_text="Required before publishing unless the image is decorative.",
        ),
        _field(("decorative",), "Decorative", EditorInputKind.BOOLEAN),
        _field(("caption",), "Caption", EditorInputKind.MARKDOWN),
        _field(("attribution", "author"), "Author"),
        _field(("attribution", "license"), "License"),
        _field(("attribution", "license_url"), "License URL", EditorInputKind.URL),
        _field(("attribution", "source_url"), "Source URL", EditorInputKind.URL),
        _field(("attribution", "credit_line"), "Credit line"),
        _field(
            ("requires_attribution",),
            "Requires attribution",
            EditorInputKind.BOOLEAN,
            help_text="Author, license and source URL are then required to publish.",
        ),
    ),
)

MEDIA_TYPE: ContentTypeDefinition[MediaData] = ContentTypeDefinition(
    key=MEDIA_CONTENT_TYPE,
    schema_version=1,
    schema=MediaSchema(),
    capabilities=frozenset(
        {
            ContentCapability.PUBLISHABLE,
            ContentCapability.PREVIEWABLE,
            ContentCapability.SEARCHABLE,
        }
    ),
    editor=MEDIA_EDITOR,
    readiness=MediaReadiness(),
    blob_fields=(BlobFieldDefinition(LinkPath.parse("file")),),
    summary_title=media_summary_title,
    embed_in_references=True,
)
