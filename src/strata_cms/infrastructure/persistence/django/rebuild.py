"""Django adapters for rebuilding link projections from authoritative state."""

from collections.abc import Iterator
from types import TracebackType
from typing import Protocol

from django.db import transaction
from django.db.models import Prefetch

from strata_cms.application.ports.clock import Clock
from strata_cms.application.ports.content_delivery import PublishedRevision
from strata_cms.application.ports.projection_rebuild import ContentRevisionHistory
from strata_cms.domain.value_objects import ContentId, ContentTypeKey
from strata_cms.infrastructure.clock.system import DjangoClock
from strata_cms.infrastructure.persistence.django.mappers import (
    revision_from_record,
)
from strata_cms.infrastructure.persistence.django.models import (
    ContentRecord,
    ContentReferenceRecord,
    ContentTitleRecord,
    MediaBlobUsageRecord,
    RevisionRecord,
)
from strata_cms.infrastructure.persistence.django.projections import (
    DjangoContentProjectionWriter,
    DjangoLinkTargetQueries,
)
from strata_cms.infrastructure.persistence.django.published_references import (
    DjangoPublishedReferenceIndex,
)

_CHUNK_SIZE = 200


class DjangoProjectionSourceQueries:
    """Stream content histories and published revisions in bounded chunks."""

    def __init__(self, *, using: str = "default") -> None:
        """Bind the reader to a Django database alias."""
        self._using = using

    def content_histories(self) -> Iterator[ContentRevisionHistory]:
        """Yield each item with its revisions; one revision query per chunk."""
        records = (
            ContentRecord.objects.using(self._using)
            .order_by("id")
            .prefetch_related(
                Prefetch(
                    "revisions",
                    queryset=RevisionRecord.objects.using(self._using).order_by(
                        "number"
                    ),
                )
            )
        )
        for record in records.iterator(chunk_size=_CHUNK_SIZE):
            yield ContentRevisionHistory(
                content_id=ContentId(record.id),
                archived=record.archived_at is not None,
                revisions=tuple(
                    revision_from_record(revision)
                    for revision in record.revisions.all()
                ),
            )

    def published_revisions(self) -> Iterator[PublishedRevision]:
        """Yield published, non-archived items with their published revision."""
        records = (
            ContentRecord.objects.using(self._using)
            .select_related("published_revision")
            .filter(
                published_revision__isnull=False,
                published_at__isnull=False,
                archived_at__isnull=True,
            )
            .order_by("id")
        )
        for record in records.iterator(chunk_size=_CHUNK_SIZE):
            if record.published_revision is None or record.published_at is None:
                continue  # pragma: no cover - excluded by the filter above
            yield PublishedRevision(
                content_id=ContentId(record.id),
                type_key=ContentTypeKey(record.type_key),
                revision=revision_from_record(record.published_revision),
                published_at=record.published_at,
            )


class DjangoProjectionReset:
    """Delete projection rows ahead of a refill in the same transaction."""

    def __init__(self, *, using: str = "default") -> None:
        """Bind the adapter to a Django database alias."""
        self._using = using

    def clear_draft_scope(self) -> None:
        """Delete every title, draft reference and blob usage row."""
        ContentTitleRecord.objects.using(self._using).all().delete()
        ContentReferenceRecord.objects.using(self._using).filter(
            scope=ContentReferenceRecord.Scope.DRAFT
        ).delete()
        MediaBlobUsageRecord.objects.using(self._using).all().delete()

    def clear_published_references(self) -> None:
        """Delete every published reference row."""
        ContentReferenceRecord.objects.using(self._using).filter(
            scope=ContentReferenceRecord.Scope.PUBLISHED
        ).delete()


class _AtomicContext(Protocol):
    """Structural type for Django's transaction context manager."""

    def __enter__(self) -> None: ...  # pragma: no cover - protocol declaration

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...  # pragma: no cover - protocol declaration


class DjangoProjectionRebuildUnitOfWork:
    """Truncate and refill one projection scope in a single transaction."""

    def __init__(self, *, clock: Clock | None = None, using: str = "default") -> None:
        """Wire source/target/projection adapters bound to one database alias."""
        self._using = using
        self.sources = DjangoProjectionSourceQueries(using=using)
        self.targets = DjangoLinkTargetQueries(using=using)
        self.reset = DjangoProjectionReset(using=using)
        self.projections = DjangoContentProjectionWriter(
            clock=clock if clock is not None else DjangoClock(),
            using=using,
        )
        self.published_references = DjangoPublishedReferenceIndex(using=using)
        self._atomic: _AtomicContext | None = None
        self._committed = False

    def __enter__(self) -> "DjangoProjectionRebuildUnitOfWork":
        """Enter a Django atomic block."""
        if self._atomic is not None:
            raise RuntimeError("A Unit of Work instance cannot be entered twice.")
        self._committed = False
        self._atomic = transaction.atomic(using=self._using)
        self._atomic.__enter__()
        return self

    def commit(self) -> None:
        """Mark the current transaction for commit on successful exit."""
        if self._atomic is None:
            raise RuntimeError("Unit of Work is not active.")
        self._committed = True

    def rollback(self) -> None:
        """Mark the current transaction for rollback."""
        if self._atomic is None:
            raise RuntimeError("Unit of Work is not active.")
        transaction.set_rollback(True, using=self._using)
        self._committed = False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Commit only explicit successful work; otherwise roll back."""
        atomic = self._atomic
        if atomic is None:  # pragma: no cover - defensive misuse guard
            raise RuntimeError("Unit of Work exit without matching enter.")

        try:
            if exc_type is not None or not self._committed:
                transaction.set_rollback(True, using=self._using)
            atomic.__exit__(exc_type, exc_value, traceback)
        finally:
            self._atomic = None
            self._committed = False
