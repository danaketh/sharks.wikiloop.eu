"""Django persistence adapter for media blob metadata."""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from strata_cms.domain.media import MediaBlob
from strata_cms.domain.value_objects import ActorId
from strata_cms.infrastructure.persistence.django.models import (
    MediaBlobRecord,
    MediaBlobUsageRecord,
)


def _to_entity(record: MediaBlobRecord) -> MediaBlob:
    if record.width is None or record.height is None:
        raise ValueError(f"Media blob record {record.pk} has no dimensions.")
    return MediaBlob(
        id=record.id,
        storage_key=record.storage_key,
        sha256=record.sha256,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        width=record.width,
        height=record.height,
        created_at=record.created_at,
        created_by=ActorId(record.created_by),
    )


class DjangoMediaBlobRepository:
    """`MediaBlobRepository` backed by `MediaBlobRecord` rows."""

    def __init__(self, *, using: str = "default") -> None:
        """Bind the repository to a Django database alias."""
        self._using = using

    def add(self, blob: MediaBlob) -> None:
        """Insert a new blob row; an existing id or storage key is an error."""
        MediaBlobRecord.objects.using(self._using).create(
            id=blob.id,
            storage_key=blob.storage_key,
            sha256=blob.sha256,
            content_type=blob.content_type,
            size_bytes=blob.size_bytes,
            width=blob.width,
            height=blob.height,
            created_at=blob.created_at,
            created_by=str(blob.created_by),
        )

    def get_many(self, ids: frozenset[UUID]) -> Mapping[UUID, MediaBlob]:
        """Return the existing blobs among ``ids`` in one query."""
        if not ids:
            return {}
        records = MediaBlobRecord.objects.using(self._using).filter(pk__in=ids)
        return {record.id: _to_entity(record) for record in records}

    def list_orphans(
        self, *, created_before: datetime, limit: int
    ) -> tuple[MediaBlob, ...]:
        """Return the oldest blobs with no usage rows created before the cutoff."""
        used = MediaBlobUsageRecord.objects.using(self._using).values("blob_id")
        records = (
            MediaBlobRecord.objects.using(self._using)
            .filter(created_at__lt=created_before)
            .exclude(pk__in=used)
            .order_by("created_at", "id")[:limit]
        )
        return tuple(_to_entity(record) for record in records)

    def delete(self, blob_id: UUID) -> None:
        """Delete one blob row; a missing row is a no-op."""
        MediaBlobRecord.objects.using(self._using).filter(pk=blob_id).delete()
