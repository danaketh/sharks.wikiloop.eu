"""Ports for rebuilding link projections from authoritative revisions."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from strata_cms.application.ports.content_delivery import PublishedRevision
from strata_cms.application.ports.links import (
    ContentProjectionWriter,
    LinkTargetQueries,
    PublishedReferenceIndex,
)
from strata_cms.application.ports.unit_of_work import UnitOfWork
from strata_cms.domain.revision import Revision
from strata_cms.domain.value_objects import ContentId


@dataclass(frozen=True, slots=True)
class ContentRevisionHistory:
    """One content item's archived flag and every revision, oldest first."""

    content_id: ContentId
    archived: bool
    revisions: tuple[Revision, ...]


class ProjectionSourceQueries(Protocol):
    """Stream authoritative state that link projections are derived from."""

    def content_histories(self) -> Iterator[ContentRevisionHistory]:
        """Yield every content item with its full revision history, batched."""
        ...  # pragma: no cover - protocol declaration

    def published_revisions(self) -> Iterator[PublishedRevision]:
        """Yield the published revision of every published, non-archived item."""
        ...  # pragma: no cover - protocol declaration


class ProjectionReset(Protocol):
    """Truncate rebuildable projections before refilling them."""

    def clear_draft_scope(self) -> None:
        """Delete every title, draft reference and blob usage row."""
        ...  # pragma: no cover - protocol declaration

    def clear_published_references(self) -> None:
        """Delete every published reference row."""
        ...  # pragma: no cover - protocol declaration


class ProjectionRebuildUnitOfWork(UnitOfWork, Protocol):
    """One transaction in which a projection scope is truncated and refilled."""

    @property
    def sources(self) -> ProjectionSourceQueries:
        """Return the authoritative revision/published-state reader."""
        ...  # pragma: no cover - protocol declaration

    @property
    def targets(self) -> LinkTargetQueries:
        """Return link target lookups reading inside this transaction."""
        ...  # pragma: no cover - protocol declaration

    @property
    def reset(self) -> ProjectionReset:
        """Return the projection truncation adapter."""
        ...  # pragma: no cover - protocol declaration

    @property
    def projections(self) -> ContentProjectionWriter:
        """Return the draft-scope projection writer."""
        ...  # pragma: no cover - protocol declaration

    @property
    def published_references(self) -> PublishedReferenceIndex:
        """Return the published reference index."""
        ...  # pragma: no cover - protocol declaration
