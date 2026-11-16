"""Typed content-reference and blob-link contracts for plugin schemas.

Links are stored in revision JSON as ``{"$ref": "<uuid>"}`` (another content
item) or ``{"$blob": "<uuid>"}`` (a stored binary). Plugins declare where links
may appear with ``LinkPath`` values; core extracts and validates them without
knowing the plugin schema. Any link object at an undeclared path is invalid.
"""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Self
from uuid import UUID

from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.plugin_api.errors import RegistryConfigurationError, SchemaDecodeError
from strata_cms.plugin_api.schema import ValidationIssue

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
    """One concrete link found in revision JSON.

    ``location`` identifies blocks by UUID; ``path`` is the ``$.``-style JSON
    path of the same value with block list indexes (``$.body[0].data.page``).
    """

    kind: Literal["reference", "blob"]
    location: str
    target: UUID
    allowed_types: frozenset[ContentTypeKey] = frozenset()
    path: str = field(kw_only=True)


def join_location(trail: tuple[str, ...]) -> str:
    """Join a key/index trail into a ``/`` location, escaping ``~`` and ``/``."""
    return "/".join(part.replace("~", "~0").replace("/", "~1") for part in trail)


def iter_link_trails(
    data: Mapping[str, object],
    path: LinkPath,
) -> Iterator[tuple[tuple[str, ...], object]]:
    """Yield ``(trail, raw value)`` for every match of ``path``."""
    yield from _walk(data, path.segments, ())


def iter_link_values(
    data: Mapping[str, object],
    path: LinkPath,
) -> Iterator[tuple[str, object]]:
    """Yield ``(concrete location, raw value)`` for every match of ``path``.

    Locations are ``/``-joined, e.g. ``gallery/2/image``. Missing keys and
    values of the wrong container type yield nothing.
    """
    for trail, value in iter_link_trails(data, path):
        yield join_location(trail), value


def _walk(
    current: object,
    segments: tuple[str, ...],
    trail: tuple[str, ...],
) -> Iterator[tuple[tuple[str, ...], object]]:
    if not segments:
        yield trail, current
        return
    head, rest = segments[0], segments[1:]
    if head == WILDCARD:
        if isinstance(current, list):
            for index, item in enumerate(current):
                yield from _walk(item, rest, (*trail, str(index)))
        return
    if isinstance(current, Mapping) and head in current:
        yield from _walk(current[head], rest, (*trail, head))


@dataclass(frozen=True, slots=True)
class LinkViolation:
    """One link-encoding violation found by :func:`find_undeclared_links`."""

    location: str
    reason: Literal["undeclared", "wrong_kind", "malformed"]


def find_undeclared_links(
    data: Mapping[str, object],
    reference_paths: Iterable[LinkPath],
    blob_paths: Iterable[LinkPath],
) -> tuple[LinkViolation, ...]:
    """Report every link-encoding violation in ``data``.

    ``reference_paths``/``blob_paths`` are the declared ``link_path`` values.
    Reported: a link object at an undeclared path (``undeclared``); a link
    object whose kind differs from the kind declared there (``wrong_kind``);
    any object with a ``$ref``/``$blob`` key that is not exactly a single-key
    link object (``malformed``), wherever it appears. Paths are compared as
    structural key/index trails, never as joined strings.

    The scan covers whatever mapping it is given. A content-level payload
    contains block envelopes whose ``data`` is governed by block definitions,
    so callers must scan only the non-block data (omit the declared block
    field subtrees) and scan each block's ``data`` separately against that
    block type's own declared paths.
    """
    declared_refs = {
        trail for path in reference_paths for trail, _ in iter_link_trails(data, path)
    }
    declared_blobs = {
        trail for path in blob_paths for trail, _ in iter_link_trails(data, path)
    }
    found: list[LinkViolation] = []
    _scan(data, (), declared_refs, declared_blobs, found)
    return tuple(found)


def _scan(
    current: object,
    trail: tuple[str, ...],
    refs: set[tuple[str, ...]],
    blobs: set[tuple[str, ...]],
    found: list[LinkViolation],
) -> None:
    if isinstance(current, Mapping):
        if REF_KEY in current or BLOB_KEY in current:
            location = join_location(trail)
            if set(current) == {REF_KEY}:
                own, other = refs, blobs
            elif set(current) == {BLOB_KEY}:
                own, other = blobs, refs
            else:
                found.append(LinkViolation(location, "malformed"))
                return
            if trail in other:
                found.append(LinkViolation(location, "wrong_kind"))
            elif trail not in own:
                found.append(LinkViolation(location, "undeclared"))
            return
        for key, child in current.items():
            _scan(child, (*trail, str(key)), refs, blobs, found)
    elif isinstance(current, list):
        for index, child in enumerate(current):
            _scan(child, (*trail, str(index)), refs, blobs, found)


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


_VIOLATION_CODES = {
    "undeclared": "undeclared_link",
    "wrong_kind": "wrong_link_kind",
    "malformed": "malformed_link",
}
COUNT_CODE = "reference_count_out_of_range"


def json_path_from_location(location: str, *, prefix: str = "$") -> str:
    """Render a ``/`` link location as a ``$.a[0].b`` JSON path under ``prefix``.

    All-digit segments are list indexes; every other key is rendered as
    ``.key``, matching the other structural validation problems.
    """
    path = prefix
    for part in location.split("/") if location else ():
        key = part.replace("~1", "/").replace("~0", "~")
        path += f"[{key}]" if key.isdigit() else f".{key}"
    return path


def link_issues(
    data: Mapping[str, object],
    references: Sequence[ReferenceFieldDefinition],
    blobs: Sequence[BlobFieldDefinition],
    *,
    prefix: str = "$",
) -> tuple[ValidationIssue, ...]:
    """Validate link encoding and reference ``max_items`` in one payload.

    ``data`` is scanned as given (see :func:`find_undeclared_links` for the
    block-envelope caveat) against the declared ``references``/``blobs``.
    Issue paths are ``$.``-style JSON paths under ``prefix``. Codes:
    ``undeclared_link``, ``wrong_link_kind``, ``malformed_link`` and
    ``reference_count_out_of_range`` (too many only; a shortfall is a
    publish-readiness issue, see :func:`link_publish_issues`).

    ``null`` at a declared single link path, or for a ``many`` field, means
    unset and is accepted; a ``null`` element inside a list is malformed.
    """
    violations = find_undeclared_links(
        data,
        [field.link_path for field in references],
        [field.link_path for field in blobs],
    )
    issues = [
        ValidationIssue(
            code=_VIOLATION_CODES[violation.reason],
            message=f"Invalid link at '{violation.location}': {violation.reason}.",
            path=json_path_from_location(violation.location, prefix=prefix),
        )
        for violation in violations
    ]
    reported = {violation.location for violation in violations}

    def malformed(location: str, message: str) -> None:
        if location not in reported:
            reported.add(location)
            issues.append(
                ValidationIssue(
                    code="malformed_link",
                    message=message,
                    path=json_path_from_location(location, prefix=prefix),
                )
            )

    for reference in references:
        for location, raw in iter_link_values(data, reference.link_path):
            if raw is None and not reference.many:
                continue
            try:
                ContentReference.from_json(raw)
            except SchemaDecodeError as exc:
                malformed(location, str(exc))
        if reference.many:
            _check_max_items(data, reference, prefix, issues, malformed)
    for blob in blobs:
        for location, raw in iter_link_values(data, blob.link_path):
            if raw is None:
                continue
            try:
                BlobReference.from_json(raw)
            except SchemaDecodeError as exc:
                malformed(location, str(exc))
    return tuple(issues)


def _check_max_items(
    data: Mapping[str, object],
    reference: ReferenceFieldDefinition,
    prefix: str,
    issues: list[ValidationIssue],
    malformed: Callable[[str, str], None],
) -> None:
    for location, raw in iter_link_values(data, reference.path):
        if raw is None:
            continue
        if not isinstance(raw, list):
            malformed(location, f"Reference list at '{location}' must be an array.")
            continue
        if reference.max_items is not None and len(raw) > reference.max_items:
            issues.append(
                ValidationIssue(
                    code=COUNT_CODE,
                    message=(
                        f"Expected at most {reference.max_items} references, "
                        f"got {len(raw)}."
                    ),
                    path=json_path_from_location(location, prefix=prefix),
                )
            )


def link_publish_issues(
    data: Mapping[str, object],
    references: Sequence[ReferenceFieldDefinition],
    *,
    prefix: str = "$",
) -> tuple[ValidationIssue, ...]:
    """Report ``min_items`` shortfalls of ``many`` reference fields.

    Absent and ``null`` fields count as zero references. This is a
    publish-readiness check: incomplete drafts remain writable.
    """
    issues: list[ValidationIssue] = []
    for reference in references:
        if not reference.many or reference.min_items == 0:
            continue
        values = list(iter_link_values(data, reference.path))
        if not values:
            values = [("/".join(reference.path.segments), None)]
        for location, raw in values:
            if raw is not None and not isinstance(raw, list):
                continue
            count = 0 if raw is None else len(raw)
            if count < reference.min_items:
                issues.append(
                    ValidationIssue(
                        code=COUNT_CODE,
                        message=(
                            f"Expected at least {reference.min_items} references, "
                            f"got {count}."
                        ),
                        path=json_path_from_location(location, prefix=prefix),
                    )
                )
    return tuple(issues)
