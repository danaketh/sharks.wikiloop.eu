"""Rebuild link projections from authoritative revisions.

Draft scope (titles, draft references, blob usage) is derived from every
revision; the published scope (published references) from each published,
non-archived item's published revision. Each scope is truncated and refilled
inside one Unit of Work, so readers see either the old or the new projection.

Links whose target content or blob no longer exists are skipped and reported
(never fabricated), and revisions that the installed content types can no
longer decode are skipped and reported, so one bad item never aborts the run.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal
from uuid import UUID

from strata_cms.application.errors import (
    ContentTypeUnavailableError,
    InvalidContentDataError,
)
from strata_cms.application.ports.content_types import (
    ContentLink,
    ContentLinks,
    ContentTypeService,
)
from strata_cms.application.ports.links import LinkTargetQueries
from strata_cms.application.ports.projection_rebuild import (
    ContentRevisionHistory,
    ProjectionRebuildUnitOfWork,
)
from strata_cms.domain.revision import Revision
from strata_cms.domain.value_objects import ContentId, RevisionId

ProjectionScope = Literal["draft", "published"]


class RebuildScope(StrEnum):
    """Which projection scope(s) to rebuild."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class SkippedLink:
    """A link left out of a projection because its target no longer exists."""

    scope: ProjectionScope
    content_id: ContentId
    revision_id: RevisionId
    kind: Literal["reference", "blob"]
    location: str
    target: UUID


@dataclass(frozen=True, slots=True)
class SkippedRevision:
    """A revision the installed content types could not decode."""

    scope: ProjectionScope
    content_id: ContentId
    revision_id: RevisionId
    reason: str


@dataclass(frozen=True, slots=True)
class ProjectionRebuildReport:
    """Counts and problems of one rebuild run."""

    draft_revisions: int = 0
    published_revisions: int = 0
    skipped_links: tuple[SkippedLink, ...] = ()
    skipped_revisions: tuple[SkippedRevision, ...] = ()


@dataclass(slots=True)
class _Progress:
    """Mutable per-scope accumulator, frozen into the report at the end."""

    revisions: int = 0
    skipped_links: list[SkippedLink] = field(default_factory=list)
    skipped_revisions: list[SkippedRevision] = field(default_factory=list)


def rebuild_projections(
    scope: RebuildScope,
    *,
    new_uow: Callable[[], ProjectionRebuildUnitOfWork],
    content_types: ContentTypeService,
) -> ProjectionRebuildReport:
    """Truncate and refill the requested scope(s), one transaction per scope."""
    draft = _Progress()
    published = _Progress()
    if scope in (RebuildScope.DRAFT, RebuildScope.ALL):
        with new_uow() as uow:
            uow.reset.clear_draft_scope()
            for history in uow.sources.content_histories():
                _rebuild_draft_history(uow, content_types, history, draft)
            uow.commit()
    if scope in (RebuildScope.PUBLISHED, RebuildScope.ALL):
        with new_uow() as uow:
            uow.reset.clear_published_references()
            for row in uow.sources.published_revisions():
                _rebuild_published(uow, content_types, row.revision, published)
            uow.commit()
    return ProjectionRebuildReport(
        draft_revisions=draft.revisions,
        published_revisions=published.revisions,
        skipped_links=(*draft.skipped_links, *published.skipped_links),
        skipped_revisions=(*draft.skipped_revisions, *published.skipped_revisions),
    )


def _rebuild_draft_history(
    uow: ProjectionRebuildUnitOfWork,
    content_types: ContentTypeService,
    history: ContentRevisionHistory,
    progress: _Progress,
) -> None:
    """Replay revisions oldest first: latest wins title/references, all blobs."""
    recorded = False
    latest_id = history.revisions[-1].id if history.revisions else None
    for revision in history.revisions:
        is_latest = revision.id == latest_id
        try:
            links = content_types.revision_links(revision)
            title = content_types.summary_title(revision) if is_latest else None
        except (InvalidContentDataError, ContentTypeUnavailableError) as exc:
            progress.skipped_revisions.append(_skipped_revision("draft", revision, exc))
            continue
        references = (
            _existing_references("draft", uow.targets, revision, links, progress)
            if is_latest
            else ()
        )
        blobs = _existing_blobs(uow.targets, revision, links, progress)
        uow.projections.record_revision(
            content_id=revision.content_id,
            revision_id=revision.id,
            type_key=revision.content_type,
            title=title,
            links=ContentLinks(references=references, blobs=blobs),
        )
        progress.revisions += 1
        recorded = True
    if recorded and history.archived:
        uow.projections.set_archived(history.content_id, True)


def _rebuild_published(
    uow: ProjectionRebuildUnitOfWork,
    content_types: ContentTypeService,
    revision: Revision,
    progress: _Progress,
) -> None:
    try:
        links = content_types.revision_links(revision)
    except (InvalidContentDataError, ContentTypeUnavailableError) as exc:
        progress.skipped_revisions.append(_skipped_revision("published", revision, exc))
        return
    references = _existing_references(
        "published", uow.targets, revision, links, progress
    )
    uow.published_references.replace(revision.content_id, revision.id, references)
    progress.revisions += 1


def _existing_references(
    scope: ProjectionScope,
    targets: LinkTargetQueries,
    revision: Revision,
    links: ContentLinks,
    progress: _Progress,
) -> tuple[ContentLink, ...]:
    if not links.references:
        return ()
    existing = targets.content_targets(
        frozenset(ContentId(link.target) for link in links.references)
    )
    return _partition(
        scope,
        revision,
        links.references,
        lambda link: ContentId(link.target) in existing,
        progress,
    )


def _existing_blobs(
    targets: LinkTargetQueries,
    revision: Revision,
    links: ContentLinks,
    progress: _Progress,
) -> tuple[ContentLink, ...]:
    if not links.blobs:
        return ()
    existing = targets.existing_blobs(frozenset(link.target for link in links.blobs))
    return _partition(
        "draft",
        revision,
        links.blobs,
        lambda link: link.target in existing,
        progress,
    )


def _partition(
    scope: ProjectionScope,
    revision: Revision,
    links: tuple[ContentLink, ...],
    exists: Callable[[ContentLink], bool],
    progress: _Progress,
) -> tuple[ContentLink, ...]:
    kept: list[ContentLink] = []
    for link in links:
        if exists(link):
            kept.append(link)
            continue
        progress.skipped_links.append(
            SkippedLink(
                scope=scope,
                content_id=revision.content_id,
                revision_id=revision.id,
                kind=link.kind,
                location=link.location,
                target=link.target,
            )
        )
    return tuple(kept)


def _skipped_revision(
    scope: ProjectionScope,
    revision: Revision,
    error: InvalidContentDataError | ContentTypeUnavailableError,
) -> SkippedRevision:
    return SkippedRevision(
        scope=scope,
        content_id=revision.content_id,
        revision_id=revision.id,
        reason=f"{type(error).__name__}: {error}",
    )
