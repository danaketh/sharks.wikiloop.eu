"""Application content-type adapter backed by the immutable Strata registry."""

from collections.abc import Mapping
from uuid import UUID

from strata_cms.application.errors import (
    ContentDataProblem,
    ContentTypeUnavailableError,
    InvalidContentDataError,
)
from strata_cms.application.ports.content_types import (
    ContentLink,
    ContentLinks,
    ContentTypeService,
    PreparedContentData,
)
from strata_cms.domain.errors import InvalidRevisionDataError
from strata_cms.domain.revision import Revision, RevisionData
from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.plugin_api.blocks import BlockFieldDefinition
from strata_cms.plugin_api.errors import (
    BlockValidationError,
    ContentTypeNotFoundError,
    ContentValidationError,
    SchemaError,
)
from strata_cms.plugin_api.links import (
    BlobReference,
    ContentReference,
    iter_link_values,
    link_issues,
    link_publish_issues,
)
from strata_cms.plugin_api.registry import RegisteredContentTypeEntry, StrataRegistry


class RegistryContentTypeService(ContentTypeService):
    """Bridge registry schemas and structured blocks into application use cases."""

    def __init__(self, registry: StrataRegistry) -> None:
        """Bind the adapter to one frozen plugin registry."""
        self._registry = registry

    def prepare_for_write(
        self,
        *,
        type_key: ContentTypeKey,
        schema_version: int,
        data: Mapping[str, object],
    ) -> PreparedContentData:
        """Normalize historical input and embedded blocks to canonical schemas."""
        entry = self._require(type_key)
        try:
            _normalized, value = self._normalize_and_decode(
                entry,
                schema_version=schema_version,
                data=RevisionData.from_mapping(data),
            )
            encoded = entry.definition.encode_untyped(value)
            canonical = self._registry.blocks.normalize_content_fields(
                encoded.data,
                entry.definition.block_fields,
            )
            problems = self._link_problems(entry, canonical)
        except (InvalidRevisionDataError, SchemaError) as exc:
            raise self._invalid_data(type_key, exc) from exc
        if problems:
            raise InvalidContentDataError(type_key=type_key, problems=problems)

        return PreparedContentData(
            schema_version=encoded.schema_version,
            data=canonical,
        )

    def validate_revision(self, revision: Revision) -> None:
        """Require installed schema/block support and semantic validity."""
        entry = self._require(revision.content_type)
        try:
            self._normalize_and_decode(
                entry,
                schema_version=revision.schema_version,
                data=revision.data,
            )
        except (InvalidRevisionDataError, SchemaError) as exc:
            raise self._invalid_data(revision.content_type, exc) from exc

    def publish_issues(self, revision: Revision) -> tuple[ContentDataProblem, ...]:
        """Evaluate publish readiness on the migrated, normalized current value."""
        entry = self._require(revision.content_type)
        try:
            normalized, value = self._normalize_and_decode(
                entry,
                schema_version=revision.schema_version,
                data=revision.data,
            )
            issues = entry.definition.publish_issues_untyped(value)
            issues += self._registry.blocks.publish_issues(
                normalized,
                entry.definition.block_fields,
            )
            issues += link_publish_issues(
                _without_block_fields(normalized, entry.definition.block_fields),
                entry.definition.reference_fields,
            )
            issues += self._registry.blocks.link_publish_issues(
                normalized,
                entry.definition.block_fields,
            )
        except (InvalidRevisionDataError, SchemaError) as exc:
            raise self._invalid_data(revision.content_type, exc) from exc
        return tuple(
            ContentDataProblem(code=i.code, message=i.message, path=i.path)
            for i in issues
        )

    def delivery_data(self, revision: Revision) -> dict[str, object]:
        """Decode a snapshot and render nested blocks for the Delivery API."""
        entry = self._require(revision.content_type)
        try:
            normalized, _value = self._normalize_and_decode(
                entry,
                schema_version=revision.schema_version,
                data=revision.data,
            )
            base = entry.definition.delivery_data(
                schema_version=entry.definition.schema_version,
                data=normalized,
            )
            return self._registry.blocks.apply_delivery_fields(
                storage_data=normalized,
                delivery_data=base,
                fields=entry.definition.block_fields,
            )
        except (InvalidRevisionDataError, SchemaError) as exc:
            raise self._invalid_data(revision.content_type, exc) from exc

    def extract_links(
        self,
        *,
        type_key: ContentTypeKey,
        data: RevisionData,
    ) -> ContentLinks:
        """Collect declared links from canonical current-version data."""
        entry = self._require(type_key)
        try:
            return self._extract_links(entry, data)
        except SchemaError as exc:
            raise self._invalid_data(type_key, exc) from exc

    def summary_title(self, revision: Revision) -> str | None:
        """Return the declared summary title of a (historical) revision."""
        entry = self._require(revision.content_type)
        if not entry.definition.has_summary_title:
            return None
        try:
            _normalized, value = self._normalize_and_decode(
                entry,
                schema_version=revision.schema_version,
                data=revision.data,
            )
            return entry.definition.summary_title_untyped(value)
        except (InvalidRevisionDataError, SchemaError) as exc:
            raise self._invalid_data(revision.content_type, exc) from exc

    def embeds_in_references(self, type_key: ContentTypeKey) -> bool:
        """Return whether the type opts into embedded reference summaries."""
        return self._require(type_key).definition.embed_in_references

    def blob_ids(self, revision: Revision) -> frozenset[UUID]:
        """Return blob ids linked from a revision after in-memory migration."""
        entry = self._require(revision.content_type)
        try:
            normalized, _value = self._normalize_and_decode(
                entry,
                schema_version=revision.schema_version,
                data=revision.data,
            )
            links = self._extract_links(entry, normalized)
        except (InvalidRevisionDataError, SchemaError) as exc:
            raise self._invalid_data(revision.content_type, exc) from exc
        return frozenset(link.target for link in links.blobs)

    def _link_problems(
        self,
        entry: RegisteredContentTypeEntry,
        canonical: RevisionData,
    ) -> tuple[ContentDataProblem, ...]:
        definition = entry.definition
        issues = link_issues(
            _without_block_fields(canonical, definition.block_fields),
            definition.reference_fields,
            definition.blob_fields,
        )
        issues += self._registry.blocks.link_issues(canonical, definition.block_fields)
        return tuple(
            ContentDataProblem(code=i.code, message=i.message, path=i.path)
            for i in issues
        )

    def _extract_links(
        self,
        entry: RegisteredContentTypeEntry,
        data: RevisionData,
    ) -> ContentLinks:
        definition = entry.definition
        plain = _without_block_fields(data, definition.block_fields)
        references: list[ContentLink] = []
        blobs: list[ContentLink] = []
        for reference in definition.reference_fields:
            for location, raw in iter_link_values(plain, reference.link_path):
                if raw is None and not reference.many:
                    continue
                references.append(
                    ContentLink(
                        "reference",
                        location,
                        ContentReference.from_json(raw).target,
                        reference.allowed_types,
                    )
                )
        for blob in definition.blob_fields:
            for location, raw in iter_link_values(plain, blob.link_path):
                if raw is None:
                    continue
                blobs.append(
                    ContentLink(
                        "blob",
                        location,
                        BlobReference.from_json(raw).blob_id,
                        frozenset(),
                    )
                )
        for occurrence in self._registry.blocks.collect_links(
            data, definition.block_fields
        ):
            link = ContentLink(
                occurrence.kind,
                occurrence.location,
                occurrence.target,
                occurrence.allowed_types,
            )
            (references if occurrence.kind == "reference" else blobs).append(link)
        return ContentLinks(references=tuple(references), blobs=tuple(blobs))

    def _normalize_and_decode(
        self,
        entry: RegisteredContentTypeEntry,
        *,
        schema_version: int,
        data: RevisionData,
    ) -> tuple[RevisionData, object]:
        migrated = entry.definition.migrate_data(
            schema_version=schema_version,
            data=data,
        )
        normalized = self._registry.blocks.normalize_content_fields(
            migrated,
            entry.definition.block_fields,
        )
        value = entry.definition.decode_untyped(
            schema_version=entry.definition.schema_version,
            data=normalized,
        )
        return normalized, value

    def _require(self, type_key: ContentTypeKey) -> RegisteredContentTypeEntry:
        try:
            return self._registry.content_types.require(type_key)
        except ContentTypeNotFoundError as exc:
            raise ContentTypeUnavailableError(type_key) from exc

    @staticmethod
    def _invalid_data(
        type_key: ContentTypeKey,
        error: InvalidRevisionDataError | SchemaError,
    ) -> InvalidContentDataError:
        if isinstance(error, (ContentValidationError, BlockValidationError)):
            problems = tuple(
                ContentDataProblem(
                    code=issue.code,
                    message=issue.message,
                    path=issue.path,
                )
                for issue in error.issues
            )
        else:
            problems = (
                ContentDataProblem(
                    code="invalid_schema",
                    message=str(error),
                ),
            )
        return InvalidContentDataError(type_key=type_key, problems=problems)


def _without_block_fields(
    data: RevisionData,
    fields: tuple[BlockFieldDefinition, ...],
) -> dict[str, object]:
    """Return content JSON with declared block-collection subtrees removed.

    Block envelopes are governed by block definitions, so content-level link
    scanning must not see them.
    """
    raw = data.as_dict()
    for field in fields:
        parent: object = raw
        for segment in field.path[:-1]:
            parent = parent.get(segment) if isinstance(parent, dict) else None
        if isinstance(parent, dict):
            parent.pop(field.path[-1], None)
    return raw
