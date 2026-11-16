"""Content write use cases shared by HTTP, Admin, CLI, and workers."""

from strata_cms.application.content.commands import (
    ArchiveContent,
    CreateContent,
    CreateRevision,
    PublishContent,
    RestoreContent,
)
from strata_cms.application.content.results import (
    ContentLifecycleResult,
    ContentWriteResult,
)
from strata_cms.application.errors import (
    ContentDataProblem,
    ContentNotFoundError,
    ContentNotReadyError,
    InvalidContentDataError,
    RevisionNotFoundError,
)
from strata_cms.application.ports.authorization import (
    ContentAction,
    ContentActor,
    ContentAuthorizationPolicy,
)
from strata_cms.application.ports.clock import Clock
from strata_cms.application.ports.content_types import (
    ContentLinks,
    ContentTypeService,
)
from strata_cms.application.ports.content_uow import ContentUnitOfWork
from strata_cms.application.ports.events import IntegrationEvent
from strata_cms.application.ports.ids import IdGenerator
from strata_cms.application.ports.links import LinkTargetQueries
from strata_cms.domain.content import Content
from strata_cms.domain.revision import Revision
from strata_cms.domain.value_objects import ContentId, ContentTypeKey, RevisionId


def create_content(  # noqa: PLR0913 - keyword-only injected ports, one per dependency
    command: CreateContent,
    *,
    uow: ContentUnitOfWork,
    clock: Clock,
    ids: IdGenerator,
    content_types: ContentTypeService,
    link_targets: LinkTargetQueries,
    policy: ContentAuthorizationPolicy,
    actor: ContentActor,
) -> ContentWriteResult:
    """Create a stable content identity and initial draft revision atomically.

    Link targets are validated before the UoW opens. The new content id does
    not exist yet, so a reference to it is reported as ``reference_not_found``.
    """
    policy.authorize(actor, ContentAction.CREATE)
    prepared = content_types.prepare_for_write(
        type_key=command.type_key,
        schema_version=command.schema_version,
        data=command.data,
    )
    links = content_types.extract_links(type_key=command.type_key, data=prepared.data)
    _require_valid_links(command.type_key, links, link_targets)
    now = clock.now()
    content = Content(
        id=ContentId(ids.new_uuid()),
        type_key=command.type_key,
        created_at=now,
        created_by=command.actor_id,
    )

    with uow:
        uow.contents.add(content)
        content, revision = content.create_revision(
            revision_id=RevisionId(ids.new_uuid()),
            schema_version=prepared.schema_version,
            data=prepared.data,
            actor_id=command.actor_id,
            now=now,
        )
        uow.revisions.add(revision)
        uow.contents.update(content, expected_version=0)
        _record_projection(uow, content_types, revision, links)
        uow.commit()

    return ContentWriteResult(
        content_id=content.id,
        revision_id=revision.id,
        content_version=content.version,
        revision_number=revision.number,
    )


def create_revision(  # noqa: PLR0913 - keyword-only injected ports, one per dependency
    command: CreateRevision,
    *,
    uow: ContentUnitOfWork,
    clock: Clock,
    ids: IdGenerator,
    content_types: ContentTypeService,
    link_targets: LinkTargetQueries,
    policy: ContentAuthorizationPolicy,
    actor: ContentActor,
) -> ContentWriteResult:
    """Append an immutable draft revision without affecting public delivery.

    Link targets are validated inside the UoW after the version check; a
    reference to the revised content itself is valid.
    """
    policy.authorize(actor, ContentAction.CHANGE)
    with uow:
        content = uow.contents.get(command.content_id)
        if content is None:
            raise ContentNotFoundError(command.content_id)

        content.require_version(command.expected_version)
        prepared = content_types.prepare_for_write(
            type_key=content.type_key,
            schema_version=command.schema_version,
            data=command.data,
        )
        content, revision = content.create_revision(
            revision_id=RevisionId(ids.new_uuid()),
            schema_version=prepared.schema_version,
            data=prepared.data,
            actor_id=command.actor_id,
            now=clock.now(),
        )
        links = content_types.extract_links(
            type_key=content.type_key, data=prepared.data
        )
        _require_valid_links(content.type_key, links, link_targets)
        uow.revisions.add(revision)
        uow.contents.update(content, expected_version=command.expected_version)
        _record_projection(uow, content_types, revision, links)
        uow.commit()

    return ContentWriteResult(
        content_id=content.id,
        revision_id=revision.id,
        content_version=content.version,
        revision_number=revision.number,
    )


def publish_content(
    command: PublishContent,
    *,
    uow: ContentUnitOfWork,
    clock: Clock,
    ids: IdGenerator,
    content_types: ContentTypeService,
    policy: ContentAuthorizationPolicy,
    actor: ContentActor,
) -> ContentWriteResult:
    """Publish a revision and persist the integration event in the same UoW."""
    policy.authorize(actor, ContentAction.PUBLISH)
    with uow:
        content = uow.contents.get(command.content_id)
        if content is None:
            raise ContentNotFoundError(command.content_id)

        revision = uow.revisions.get(command.revision_id)
        if revision is None:
            raise RevisionNotFoundError(command.revision_id)

        content.require_version(command.expected_version)
        content_types.validate_revision(revision)
        problems = content_types.publish_issues(revision)
        if problems:
            raise ContentNotReadyError(
                type_key=revision.content_type,
                problems=problems,
            )
        now = clock.now()
        content, changed = content.publish(
            revision,
            actor_id=command.actor_id,
            now=now,
        )
        if changed:
            uow.contents.update(
                content,
                expected_version=command.expected_version,
            )
            uow.events.publish(
                IntegrationEvent(
                    id=ids.new_uuid(),
                    type="strata.content.published",
                    version=1,
                    payload={
                        "content_id": str(content.id),
                        "revision_id": str(revision.id),
                        "content_type": str(content.type_key),
                        "content_version": content.version,
                        "actor_id": str(command.actor_id),
                    },
                    occurred_at=now,
                )
            )
        uow.commit()

    return ContentWriteResult(
        content_id=content.id,
        revision_id=revision.id,
        content_version=content.version,
        revision_number=revision.number,
    )


def archive_content(
    command: ArchiveContent,
    *,
    uow: ContentUnitOfWork,
    clock: Clock,
    ids: IdGenerator,
    policy: ContentAuthorizationPolicy,
    actor: ContentActor,
) -> ContentLifecycleResult:
    """Archive content, blocking further edits/publishes until restored."""
    policy.authorize(actor, ContentAction.ARCHIVE)
    with uow:
        content = uow.contents.get(command.content_id)
        if content is None:
            raise ContentNotFoundError(command.content_id)

        content.require_version(command.expected_version)
        now = clock.now()
        content, changed = content.archive(actor_id=command.actor_id, now=now)
        if changed:
            uow.contents.update(content, expected_version=command.expected_version)
            uow.projections.set_archived(content.id, True)
            uow.events.publish(
                IntegrationEvent(
                    id=ids.new_uuid(),
                    type="strata.content.archived",
                    version=1,
                    payload={
                        "content_id": str(content.id),
                        "content_type": str(content.type_key),
                        "content_version": content.version,
                        "actor_id": str(command.actor_id),
                    },
                    occurred_at=now,
                )
            )
        uow.commit()

    return ContentLifecycleResult(
        content_id=content.id,
        content_version=content.version,
        is_archived=True,
    )


def restore_content(
    command: RestoreContent,
    *,
    uow: ContentUnitOfWork,
    clock: Clock,
    ids: IdGenerator,
    policy: ContentAuthorizationPolicy,
    actor: ContentActor,
) -> ContentLifecycleResult:
    """Restore previously archived content to an editable/publishable state."""
    policy.authorize(actor, ContentAction.RESTORE)
    with uow:
        content = uow.contents.get(command.content_id)
        if content is None:
            raise ContentNotFoundError(command.content_id)

        content.require_version(command.expected_version)
        now = clock.now()
        content, changed = content.restore()
        if changed:
            uow.contents.update(content, expected_version=command.expected_version)
            uow.projections.set_archived(content.id, False)
            uow.events.publish(
                IntegrationEvent(
                    id=ids.new_uuid(),
                    type="strata.content.restored",
                    version=1,
                    payload={
                        "content_id": str(content.id),
                        "content_type": str(content.type_key),
                        "content_version": content.version,
                        "actor_id": str(command.actor_id),
                    },
                    occurred_at=now,
                )
            )
        uow.commit()

    return ContentLifecycleResult(
        content_id=content.id,
        content_version=content.version,
        is_archived=False,
    )


def _require_valid_links(
    type_key: ContentTypeKey,
    links: ContentLinks,
    link_targets: LinkTargetQueries,
) -> None:
    """Reject links to missing/archived/disallowed content or missing blobs."""
    problems = _link_problems(links, link_targets)
    if problems:
        raise InvalidContentDataError(type_key=type_key, problems=problems)


def _link_problems(
    links: ContentLinks,
    link_targets: LinkTargetQueries,
) -> tuple[ContentDataProblem, ...]:
    """Check every link against one batched target and one batched blob lookup."""
    targets = link_targets.content_targets(
        frozenset(ContentId(link.target) for link in links.references)
    )
    existing_blobs = link_targets.existing_blobs(
        frozenset(link.target for link in links.blobs)
    )
    problems: list[ContentDataProblem] = []
    for link in links.references:
        target = targets.get(ContentId(link.target))
        if target is None:
            problems.append(
                ContentDataProblem(
                    code="reference_not_found",
                    message=f"Referenced content '{link.target}' does not exist.",
                    path=link.path,
                )
            )
        elif target.archived:
            problems.append(
                ContentDataProblem(
                    code="reference_archived",
                    message=f"Referenced content '{link.target}' is archived.",
                    path=link.path,
                )
            )
        elif target.type_key not in link.allowed_types:
            allowed = ", ".join(sorted(str(key) for key in link.allowed_types))
            problems.append(
                ContentDataProblem(
                    code="reference_type_not_allowed",
                    message=(
                        f"Referenced content '{link.target}' has type "
                        f"'{target.type_key}'; allowed: {allowed}."
                    ),
                    path=link.path,
                )
            )
    problems.extend(
        ContentDataProblem(
            code="blob_not_found",
            message=f"Blob '{link.target}' does not exist.",
            path=link.path,
        )
        for link in links.blobs
        if link.target not in existing_blobs
    )
    return tuple(problems)


def _record_projection(
    uow: ContentUnitOfWork,
    content_types: ContentTypeService,
    revision: Revision,
    links: ContentLinks,
) -> None:
    """Write draft-scope projections in the same transaction as the revision."""
    uow.projections.record_revision(
        content_id=revision.content_id,
        revision_id=revision.id,
        type_key=revision.content_type,
        title=content_types.summary_title(revision),
        links=links,
    )
