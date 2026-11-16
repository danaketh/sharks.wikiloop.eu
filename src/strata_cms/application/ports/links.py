"""Ports for write-time link target lookups and draft-scope link projections."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from strata_cms.application.ports.content_types import ContentLinks
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
