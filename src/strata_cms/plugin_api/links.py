"""Typed content-reference and blob-link contracts for plugin schemas.

Links are stored in revision JSON as ``{"$ref": "<uuid>"}`` (another content
item) or ``{"$blob": "<uuid>"}`` (a stored binary). Plugins declare where links
may appear with ``LinkPath`` values; core extracts and validates them without
knowing the plugin schema. Any link object at an undeclared path is invalid.
"""

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, Self
from uuid import UUID

from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.plugin_api.errors import RegistryConfigurationError, SchemaDecodeError

REF_KEY = "$ref"
BLOB_KEY = "$blob"
WILDCARD = "*"
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOKEN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)((?:\[\*\])*)$")


@dataclass(frozen=True, slots=True)
class LinkPath:
    """JSON path to a link value; ``*`` segments match every list item."""

    segments: tuple[str, ...]

    def __post_init__(self) -> None:
        """Require a leading object key and identifier-like keys or wildcards."""
        if not self.segments or self.segments[0] == WILDCARD:
            raise RegistryConfigurationError(
                "Link paths must start with an object key."
            )
        for segment in self.segments:
            if segment != WILDCARD and not _KEY.fullmatch(segment):
                raise RegistryConfigurationError(
                    f"Invalid link path segment '{segment}'."
                )

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse ``gallery[*].image`` into ``("gallery", "*", "image")``."""
        segments: list[str] = []
        for token in text.split("."):
            match = _TOKEN.fullmatch(token)
            if match is None:
                raise RegistryConfigurationError(f"Invalid link path '{text}'.")
            segments.append(match.group(1))
            segments.extend([WILDCARD] * (len(match.group(2)) // 3))
        return cls(tuple(segments))

    def with_wildcard(self) -> Self:
        """Return this path extended to every item of the list it points at."""
        return type(self)((*self.segments, WILDCARD))

    def __str__(self) -> str:
        """Render the canonical ``a[*].b`` text form."""
        rendered = ""
        for segment in self.segments:
            if segment == WILDCARD:
                rendered += "[*]"
            else:
                rendered += f".{segment}" if rendered else segment
        return rendered


@dataclass(frozen=True, slots=True)
class ReferenceFieldDefinition:
    """Declares a typed reference to other content items at one JSON path.

    When ``many`` is true the value at ``path`` is a list of reference objects
    and ``min_items``/``max_items`` constrain its length.
    """

    path: LinkPath
    allowed_types: frozenset[ContentTypeKey]
    many: bool = False
    min_items: int = 0
    max_items: int | None = None

    def __post_init__(self) -> None:
        """Reject impossible reference declarations at plugin registration."""
        if not self.allowed_types:
            raise RegistryConfigurationError(
                f"Reference field '{self.path}' must allow at least one type."
            )
        if self.min_items < 0:
            raise RegistryConfigurationError("Reference min_items cannot be negative.")
        if self.max_items is not None and self.max_items < self.min_items:
            raise RegistryConfigurationError(
                "Reference max_items cannot be smaller than min_items."
            )
        if not self.many and (self.min_items or self.max_items is not None):
            raise RegistryConfigurationError(
                "Reference min_items/max_items require many=True."
            )

    @property
    def link_path(self) -> LinkPath:
        """Return the path at which individual reference objects live."""
        return self.path.with_wildcard() if self.many else self.path


@dataclass(frozen=True, slots=True)
class BlobFieldDefinition:
    """Declares a link to a stored binary at one JSON path."""

    path: LinkPath

    @property
    def link_path(self) -> LinkPath:
        """Return the path at which the blob object lives."""
        return self.path


def _parse_uuid(raw: object, *, key: str, what: str) -> UUID:
    if not isinstance(raw, Mapping) or set(raw) != {key}:
        raise SchemaDecodeError(f"{what} must be an object with only '{key}'.")
    value = raw[key]
    if not isinstance(value, str):
        raise SchemaDecodeError(f"{what} id must be a UUID string.")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise SchemaDecodeError(f"{what} id must be a valid UUID.") from exc
    if str(parsed) != value:
        raise SchemaDecodeError(f"{what} id must be a canonical lowercase UUID.")
    return parsed


@dataclass(frozen=True, slots=True)
class ContentReference:
    """Persisted pointer to another content item by stable UUID."""

    target: UUID

    def to_json(self) -> dict[str, str]:
        """Return the persisted ``{"$ref": uuid}`` encoding."""
        return {REF_KEY: str(self.target)}

    @classmethod
    def from_json(cls, raw: object) -> Self:
        """Parse an untrusted persisted reference."""
        return cls(_parse_uuid(raw, key=REF_KEY, what="Content reference"))


@dataclass(frozen=True, slots=True)
class BlobReference:
    """Persisted pointer to a stored blob by stable UUID."""

    blob_id: UUID

    def to_json(self) -> dict[str, str]:
        """Return the persisted ``{"$blob": uuid}`` encoding."""
        return {BLOB_KEY: str(self.blob_id)}

    @classmethod
    def from_json(cls, raw: object) -> Self:
        """Parse an untrusted persisted blob link."""
        return cls(_parse_uuid(raw, key=BLOB_KEY, what="Blob reference"))


@dataclass(frozen=True, slots=True)
class LinkOccurrence:
    """One concrete link found in revision JSON."""

    kind: Literal["reference", "blob"]
    location: str
    target: UUID


def iter_link_values(
    data: Mapping[str, object],
    path: LinkPath,
) -> Iterator[tuple[str, object]]:
    """Yield ``(concrete location, raw value)`` for every match of ``path``.

    Locations are ``/``-joined, e.g. ``gallery/2/image``. Missing keys and
    values of the wrong container type yield nothing.
    """
    yield from _walk(data, path.segments, ())


def _walk(
    current: object,
    segments: tuple[str, ...],
    trail: tuple[str, ...],
) -> Iterator[tuple[str, object]]:
    if not segments:
        yield "/".join(trail), current
        return
    head, rest = segments[0], segments[1:]
    if head == WILDCARD:
        if isinstance(current, list):
            for index, item in enumerate(current):
                yield from _walk(item, rest, (*trail, str(index)))
        return
    if isinstance(current, Mapping) and head in current:
        yield from _walk(current[head], rest, (*trail, head))


def find_undeclared_links(
    data: Mapping[str, object],
    declared: Iterable[LinkPath],
) -> tuple[str, ...]:
    """Return locations of ``$ref``/``$blob`` objects outside declared paths."""
    allowed = {
        location
        for path in declared
        for location, _value in iter_link_values(data, path)
    }
    found: list[str] = []
    _scan(data, (), allowed, found)
    return tuple(found)


def _scan(
    current: object,
    trail: tuple[str, ...],
    allowed: set[str],
    found: list[str],
) -> None:
    if isinstance(current, Mapping):
        if REF_KEY in current or BLOB_KEY in current:
            location = "/".join(trail)
            if location not in allowed:
                found.append(location)
            return
        for key, child in current.items():
            _scan(child, (*trail, str(key)), allowed, found)
    elif isinstance(current, list):
        for index, child in enumerate(current):
            _scan(child, (*trail, str(index)), allowed, found)


def require_unique_link_paths(
    owner: str,
    references: Iterable[ReferenceFieldDefinition],
    blobs: Iterable[BlobFieldDefinition],
) -> None:
    """Reject duplicate link declarations, including a path used as both kinds."""
    paths = [field.path for field in references] + [field.path for field in blobs]
    if len(paths) != len(set(paths)):
        raise RegistryConfigurationError(
            f"{owner} declares a link field path more than once."
        )
