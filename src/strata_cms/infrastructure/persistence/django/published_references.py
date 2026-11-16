"""Django adapter for the published-scope reverse reference index."""

from django.db import transaction

from strata_cms.application.ports.content_types import ContentLink
from strata_cms.domain.value_objects import ContentId, ContentTypeKey, RevisionId
from strata_cms.infrastructure.persistence.django.models import (
    ContentRecord,
    ContentReferenceRecord,
)

_PUBLISHED = ContentReferenceRecord.Scope.PUBLISHED


class DjangoPublishedReferenceIndex:
    """Published reference rows in ``strata_content_reference``.

    ``replace`` runs atomically (a savepoint inside an outer transaction) and
    locks the source content row so concurrent workers handling events for
    the same source serialize instead of interleaving delete/insert.
    """

    def __init__(self, *, using: str = "default") -> None:
        """Bind the adapter to a Django database alias."""
        self._using = using

    def replace(
        self,
        source_id: ContentId,
        revision_id: RevisionId,
        links: tuple[ContentLink, ...],
    ) -> None:
        """Replace the source's published rows; skip links to missing targets.

        No-op when ``revision_id`` is no longer the source's current published
        revision (or the source is archived): the event that changed that
        state reconciles the rows, so a slow, stale handler cannot win.
        """
        with transaction.atomic(using=self._using):
            current = (
                ContentRecord.objects.using(self._using)
                .select_for_update()
                .filter(pk=source_id)
                .values_list("published_revision_id", "archived_at")
                .first()
            )
            if current is None or current != (revision_id, None):
                return
            rows = ContentReferenceRecord.objects.using(self._using)
            rows.filter(scope=_PUBLISHED, source_id=source_id).delete()
            references = [link for link in links if link.kind == "reference"]
            if not references:
                return
            target_types = dict(
                ContentRecord.objects.using(self._using)
                .filter(pk__in={link.target for link in references})
                .values_list("id", "type_key")
            )
            rows.bulk_create(
                [
                    ContentReferenceRecord(
                        scope=_PUBLISHED,
                        source_id=source_id,
                        source_revision_id=revision_id,
                        location=link.location,
                        target_id=link.target,
                        target_type=target_types[link.target],
                    )
                    for link in references
                    if link.target in target_types
                ]
            )

    def remove(self, source_id: ContentId) -> None:
        """Delete every published row of ``source_id``."""
        ContentReferenceRecord.objects.using(self._using).filter(
            scope=_PUBLISHED, source_id=source_id
        ).delete()

    def referencing(
        self,
        target_id: ContentId,
        *,
        source_type: ContentTypeKey | None,
        limit: int,
        offset: int,
    ) -> tuple[tuple[ContentId, ...], int]:
        """Page currently published, non-archived sources, newest first."""
        source_ids = (
            ContentReferenceRecord.objects.using(self._using)
            .filter(scope=_PUBLISHED, target_id=target_id)
            .values("source_id")
        )
        sources = ContentRecord.objects.using(self._using).filter(
            pk__in=source_ids,
            published_revision__isnull=False,
            published_at__isnull=False,
            archived_at__isnull=True,
        )
        if source_type is not None:
            sources = sources.filter(type_key=str(source_type))
        total = sources.count()
        page = sources.order_by("-published_at", "-id").values_list("id", flat=True)[
            offset : offset + limit
        ]
        return tuple(ContentId(pk) for pk in page), total
