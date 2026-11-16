"""Django adapters for link target lookups and draft-scope link projections."""

from collections.abc import Mapping
from uuid import UUID

from django.db import connections

from strata_cms.application.ports.clock import Clock
from strata_cms.application.ports.content_types import ContentLinks
from strata_cms.application.ports.links import LinkTarget
from strata_cms.domain.value_objects import ContentId, ContentTypeKey, RevisionId
from strata_cms.infrastructure.persistence.django.models import (
    TITLE_MAX_LENGTH,
    ContentRecord,
    ContentReferenceRecord,
    ContentTitleRecord,
    MediaBlobRecord,
    MediaBlobUsageRecord,
)


def _projected_title(title: str | None) -> str | None:
    """Strip and truncate so a long summary title never fails a revision write."""
    if title is None:
        return None
    return title.strip()[:TITLE_MAX_LENGTH]


class DjangoLinkTargetQueries:
    """Batched reads of authoritative content/blob state for link validation."""

    def __init__(self, *, using: str = "default") -> None:
        """Bind the adapter to a Django database alias."""
        self._using = using

    def content_targets(
        self, ids: frozenset[ContentId]
    ) -> Mapping[ContentId, LinkTarget]:
        """Return the existing content items among ``ids`` in one query."""
        if not ids:
            return {}
        rows = (
            ContentRecord.objects.using(self._using)
            .filter(pk__in=ids)
            .values_list("id", "type_key", "archived_at")
        )
        return {
            ContentId(pk): LinkTarget(
                content_id=ContentId(pk),
                type_key=ContentTypeKey(type_key),
                archived=archived_at is not None,
            )
            for pk, type_key, archived_at in rows
        }

    def existing_blobs(self, ids: frozenset[UUID]) -> frozenset[UUID]:
        """Return the stored blob ids among ``ids`` in one query."""
        if not ids:
            return frozenset()
        return frozenset(
            MediaBlobRecord.objects.using(self._using)
            .filter(pk__in=ids)
            .values_list("id", flat=True)
        )


class DjangoContentProjectionWriter:
    """Write title/draft-reference/blob-usage projections in the caller's tx.

    Must be used inside the content Unit of Work's atomic block so the rows
    commit or roll back with the authoritative revision write.
    """

    def __init__(self, *, clock: Clock, using: str = "default") -> None:
        """Bind the writer to a clock and a Django database alias."""
        self._clock = clock
        self._using = using

    def record_revision(
        self,
        *,
        content_id: ContentId,
        revision_id: RevisionId,
        type_key: ContentTypeKey,
        title: str | None,
        links: ContentLinks,
    ) -> None:
        """Upsert the title, replace draft references, append blob usage."""
        ContentTitleRecord.objects.using(self._using).update_or_create(
            content_id=content_id,
            defaults={
                "type_key": str(type_key),
                "title": _projected_title(title),
                "updated_at": self._clock.now(),
            },
        )
        self._replace_draft_references(content_id, revision_id, links)
        self.record_blob_usage(
            content_id=content_id,
            revision_id=revision_id,
            blob_ids=frozenset(link.target for link in links.blobs),
        )

    def record_blob_usage(
        self,
        *,
        content_id: ContentId,
        revision_id: RevisionId,
        blob_ids: frozenset[UUID],
    ) -> None:
        """Lock the used blob rows, then append one usage row per blob.

        The lock serializes with orphan cleanup (``delete_if_unused``): a blob
        deleted after link validation fails this write instead of leaving a
        revision pointing at a deleted blob.
        """
        if not blob_ids:
            return
        ordered = sorted(blob_ids)
        features = connections[self._using].features
        locked = set(
            MediaBlobRecord.objects.using(self._using)
            .select_for_update(no_key=features.has_select_for_no_key_update)
            .filter(pk__in=ordered)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        missing = blob_ids - locked
        if missing:
            raise LookupError(f"Blobs {sorted(map(str, missing))} do not exist.")
        MediaBlobUsageRecord.objects.using(self._using).bulk_create(
            [
                MediaBlobUsageRecord(
                    blob_id=blob_id,
                    content_id=content_id,
                    revision_id=revision_id,
                )
                for blob_id in ordered
            ],
            ignore_conflicts=True,
        )

    def set_archived(self, content_id: ContentId, archived: bool) -> None:
        """Mirror the archived flag; a missing row is left to a rebuild."""
        ContentTitleRecord.objects.using(self._using).filter(pk=content_id).update(
            archived=archived,
            updated_at=self._clock.now(),
        )

    def _replace_draft_references(
        self,
        content_id: ContentId,
        revision_id: RevisionId,
        links: ContentLinks,
    ) -> None:
        references = ContentReferenceRecord.objects.using(self._using)
        references.filter(
            scope=ContentReferenceRecord.Scope.DRAFT,
            source_id=content_id,
        ).delete()
        if not links.references:
            return
        target_types = dict(
            ContentRecord.objects.using(self._using)
            .filter(pk__in={link.target for link in links.references})
            .values_list("id", "type_key")
        )
        missing = {link.target for link in links.references} - target_types.keys()
        if missing:
            raise LookupError(
                f"Reference targets {sorted(map(str, missing))} do not exist."
            )
        references.bulk_create(
            [
                ContentReferenceRecord(
                    scope=ContentReferenceRecord.Scope.DRAFT,
                    source_id=content_id,
                    source_revision_id=revision_id,
                    location=link.location,
                    target_id=link.target,
                    target_type=target_types[link.target],
                )
                for link in links.references
            ]
        )
