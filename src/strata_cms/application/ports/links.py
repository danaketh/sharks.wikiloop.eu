"""Ports for link target lookups, draft-scope projections and Delivery links."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from strata_cms.application.ports.content_delivery import PublishedRevision
from strata_cms.application.ports.content_types import ContentLink, ContentLinks
from strata_cms.domain.value_objects import ContentId, ContentTypeKey, RevisionId


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """Authoritative state of one reference target needed to validate a link."""

    content_id: ContentId
    type_key: ContentTypeKey
    archived: bool


class LinkTargetQueries(Protocol):
    """Batched lookups of reference targets and stored blobs."""

    def content_targets(
        self, ids: frozenset[ContentId]
    ) -> Mapping[ContentId, LinkTarget]:
        """Return the existing content items among ``ids`` (missing ids omitted)."""
        ...  # pragma: no cover - protocol declaration

    def existing_blobs(self, ids: frozenset[UUID]) -> frozenset[UUID]:
        """Return the subset of ``ids`` that name stored media blobs."""
        ...  # pragma: no cover - protocol declaration


class ContentProjectionWriter(Protocol):
    """Rebuildable draft-scope projections written inside the content UoW."""

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
        ...  # pragma: no cover - protocol declaration

    def set_archived(self, content_id: ContentId, archived: bool) -> None:
        """Mirror the content's archived flag into the title projection."""
        ...  # pragma: no cover - protocol declaration


class PublishedLinkQueries(Protocol):
    """Batched published-state lookup of Delivery reference targets."""

    def published_revisions(
        self, ids: frozenset[ContentId]
    ) -> Mapping[ContentId, PublishedRevision]:
        """Return published, non-archived targets among ``ids`` in one query.

        Unpublished, archived and missing ids are omitted; draft state is never
        returned.
        """
        ...  # pragma: no cover - protocol declaration


class PublishedReferenceIndex(Protocol):
    """Rebuildable published-scope reverse reference index.

    Rows are derived from each source's published revision. Readers exclude
    sources that are not currently published, so a stale row never leaks.
    """

    def replace(
        self,
        source_id: ContentId,
        revision_id: RevisionId,
        links: tuple[ContentLink, ...],
    ) -> None:
        """Replace every published row of ``source_id`` with ``links``.

        Reference links whose target no longer exists are not indexed: a row
        cannot be derived for them without fabricating the target's type. A
        no-op when ``revision_id`` is no longer the source's current published
        revision, so a stale, redelivered handler cannot overwrite newer rows.
        """
        ...  # pragma: no cover - protocol declaration

    def remove(self, source_id: ContentId) -> None:
        """Delete every published row of ``source_id`` (no-op when absent)."""
        ...  # pragma: no cover - protocol declaration

    def referencing(
        self,
        target_id: ContentId,
        *,
        source_type: ContentTypeKey | None,
        limit: int,
        offset: int,
    ) -> tuple[tuple[ContentId, ...], int]:
        """Return one page of currently published sources referencing a target.

        Sources are distinct and ordered newest-published first; the int is the
        total number of matching sources.
        """
        ...  # pragma: no cover - protocol declaration
