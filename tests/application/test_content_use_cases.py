import copy
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from uuid import UUID

import pytest

from strata_cms.application.content.commands import (
    ArchiveContent,
    CreateContent,
    CreateRevision,
    PublishContent,
    RestoreContent,
)
from strata_cms.application.content.use_cases import (
    archive_content,
    create_content,
    create_revision,
    publish_content,
    restore_content,
)
from strata_cms.application.errors import (
    ContentDataProblem,
    ContentNotFoundError,
    ContentNotReadyError,
    InvalidContentDataError,
    RevisionNotFoundError,
)
from strata_cms.application.ports.authorization import ContentAction, ContentActor
from strata_cms.application.ports.content_types import (
    ContentLink,
    ContentLinks,
    PreparedContentData,
)
from strata_cms.application.ports.events import IntegrationEvent
from strata_cms.application.ports.links import LinkTarget
from strata_cms.domain.content import Content
from strata_cms.domain.errors import ConcurrentModificationError, ContentArchivedError
from strata_cms.domain.revision import Revision, RevisionData
from strata_cms.domain.value_objects import (
    ActorId,
    ContentId,
    ContentTypeKey,
    RevisionId,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
ACTOR = ActorId("user:42")
ACTOR_CONTEXT = ContentActor(id=ACTOR, is_staff=True, permissions=frozenset())


class AllowAllPolicy:
    """Permissive fake policy: these tests exercise use-case behavior, not authz."""

    def authorize(self, actor: ContentActor, action: ContentAction) -> None:
        del actor, action


POLICY = AllowAllPolicy()


class FakeContentTypes:
    def __init__(self) -> None:
        self.readiness_problems: tuple[ContentDataProblem, ...] = ()
        self.readiness_calls = 0

    def prepare_for_write(
        self,
        *,
        type_key: ContentTypeKey,
        schema_version: int,
        data: Mapping[str, object],
    ) -> PreparedContentData:
        del type_key
        return PreparedContentData(
            schema_version=schema_version,
            data=RevisionData.from_mapping(data),
        )

    def validate_revision(self, revision: Revision) -> None:
        del revision

    def publish_issues(self, revision: Revision) -> tuple[ContentDataProblem, ...]:
        del revision
        self.readiness_calls += 1
        return self.readiness_problems

    def delivery_data(self, revision: Revision) -> dict[str, object]:
        return revision.data.as_dict()

    def extract_links(
        self, *, type_key: ContentTypeKey, data: RevisionData
    ) -> ContentLinks:
        del type_key, data
        return ContentLinks()

    def summary_title(self, revision: Revision) -> str | None:
        del revision
        return None

    def embeds_in_references(self, type_key: ContentTypeKey) -> bool:
        del type_key
        return False

    def revision_links(self, revision: Revision) -> ContentLinks:
        del revision
        return ContentLinks()

    def blob_ids(self, revision: Revision) -> frozenset[UUID]:
        del revision
        return frozenset()


CONTENT_TYPES = FakeContentTypes()


class FrozenClock:
    def now(self) -> datetime:
        return NOW


class SequenceIds:
    def __init__(self, *values: int) -> None:
        self._values = iter(values)

    def new_uuid(self) -> UUID:
        return UUID(int=next(self._values))


class FakeContentRepository:
    def __init__(self) -> None:
        self.items: dict[ContentId, Content] = {}

    def get(self, content_id: ContentId) -> Content | None:
        item = self.items.get(content_id)
        return copy.deepcopy(item) if item is not None else None

    def add(self, content: Content) -> None:
        self.items[content.id] = copy.deepcopy(content)

    def update(self, content: Content, *, expected_version: int) -> None:
        persisted = self.items.get(content.id)
        if persisted is None or persisted.version != expected_version:
            raise ConcurrentModificationError("stale fake aggregate")
        self.items[content.id] = copy.deepcopy(content)


class FakeRevisionRepository:
    def __init__(self) -> None:
        self.items: dict[RevisionId, Revision] = {}

    def get(self, revision_id: RevisionId) -> Revision | None:
        return self.items.get(revision_id)

    def add(self, revision: Revision) -> None:
        self.items[revision.id] = revision


class FakeEvents:
    def __init__(self) -> None:
        self.items: list[IntegrationEvent] = []

    def publish(self, event: IntegrationEvent) -> None:
        self.items.append(event)


@dataclass(frozen=True)
class RecordedRevision:
    content_id: ContentId
    revision_id: RevisionId
    type_key: ContentTypeKey
    title: str | None
    links: ContentLinks


class FakeProjections:
    def __init__(self) -> None:
        self.revisions: list[RecordedRevision] = []
        self.archived: dict[ContentId, bool] = {}

    def record_revision(
        self,
        *,
        content_id: ContentId,
        revision_id: RevisionId,
        type_key: ContentTypeKey,
        title: str | None,
        links: ContentLinks,
    ) -> None:
        self.revisions.append(
            RecordedRevision(content_id, revision_id, type_key, title, links)
        )

    def set_archived(self, content_id: ContentId, archived: bool) -> None:
        self.archived[content_id] = archived


class FakeLinkTargets:
    def __init__(
        self,
        targets: Mapping[ContentId, LinkTarget] | None = None,
        blobs: frozenset[UUID] = frozenset(),
    ) -> None:
        self.targets = dict(targets or {})
        self.blobs = blobs

    def content_targets(
        self, ids: frozenset[ContentId]
    ) -> Mapping[ContentId, LinkTarget]:
        return {i: self.targets[i] for i in ids if i in self.targets}

    def existing_blobs(self, ids: frozenset[UUID]) -> frozenset[UUID]:
        return ids & self.blobs


LINK_TARGETS = FakeLinkTargets()


class UowLinkTargets(FakeLinkTargets):
    """Resolve targets from the fake UoW's contents, like the real adapter."""

    def __init__(self, uow: "FakeContentUnitOfWork") -> None:
        super().__init__()
        self._uow = uow

    def content_targets(
        self, ids: frozenset[ContentId]
    ) -> Mapping[ContentId, LinkTarget]:
        return {
            i: LinkTarget(
                content_id=i,
                type_key=item.type_key,
                archived=item.archived_at is not None,
            )
            for i, item in self._uow.contents.items.items()
            if i in ids
        }


class LinkingContentTypes(FakeContentTypes):
    def __init__(self, links: ContentLinks, title: str | None = None) -> None:
        super().__init__()
        self.links = links
        self.title = title

    def extract_links(
        self, *, type_key: ContentTypeKey, data: RevisionData
    ) -> ContentLinks:
        del type_key, data
        return self.links

    def summary_title(self, revision: Revision) -> str | None:
        del revision
        return self.title


class FakeContentUnitOfWork:
    def __init__(self) -> None:
        self.contents = FakeContentRepository()
        self.revisions = FakeRevisionRepository()
        self.events = FakeEvents()
        self.projections = FakeProjections()
        self.committed = False

    def __enter__(self) -> "FakeContentUnitOfWork":
        self.committed = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.committed = False


def test_create_content_persists_identity_and_initial_revision() -> None:
    uow = FakeContentUnitOfWork()

    result = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={"title": "First"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    persisted = uow.contents.get(result.content_id)
    revision = uow.revisions.get(result.revision_id)
    assert persisted is not None
    assert revision is not None
    assert persisted.latest_revision_id == result.revision_id
    assert persisted.published_revision_id is None
    assert result.content_version == 1
    assert result.revision_number == 1
    assert uow.committed is True


def test_create_revision_preserves_published_pointer() -> None:
    uow = FakeContentUnitOfWork()
    created = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={"title": "First"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    second = create_revision(
        CreateRevision(
            content_id=created.content_id,
            expected_version=created.content_version,
            schema_version=2,
            data={"title": "Second"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(3),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    persisted = uow.contents.get(created.content_id)
    assert persisted is not None
    assert persisted.latest_revision_id == second.revision_id
    assert persisted.published_revision_id is None
    assert second.revision_number == 2
    assert second.content_version == 2


def test_create_revision_rejects_stale_content_version() -> None:
    uow = FakeContentUnitOfWork()
    created = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    with pytest.raises(ConcurrentModificationError):
        create_revision(
            CreateRevision(
                content_id=created.content_id,
                expected_version=0,
                schema_version=1,
                data={},
                actor_id=ACTOR,
            ),
            uow=uow,
            clock=FrozenClock(),
            ids=SequenceIds(3),
            content_types=CONTENT_TYPES,
            link_targets=LINK_TARGETS,
            policy=POLICY,
            actor=ACTOR_CONTEXT,
        )


def test_publish_updates_pointer_and_writes_integration_event() -> None:
    uow = FakeContentUnitOfWork()
    created = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={"title": "First"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    published = publish_content(
        PublishContent(
            content_id=created.content_id,
            revision_id=created.revision_id,
            expected_version=created.content_version,
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(3),
        content_types=CONTENT_TYPES,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    persisted = uow.contents.get(created.content_id)
    assert persisted is not None
    assert persisted.published_revision_id == created.revision_id
    assert published.content_version == 2
    assert len(uow.events.items) == 1
    event = uow.events.items[0]
    assert event.type == "strata.content.published"
    assert event.payload["revision_id"] == str(created.revision_id)


def test_create_revision_rejects_missing_content() -> None:
    uow = FakeContentUnitOfWork()

    with pytest.raises(ContentNotFoundError):
        create_revision(
            CreateRevision(
                content_id=ContentId(UUID(int=404)),
                expected_version=0,
                schema_version=1,
                data={},
                actor_id=ACTOR,
            ),
            uow=uow,
            clock=FrozenClock(),
            ids=SequenceIds(1),
            content_types=CONTENT_TYPES,
            link_targets=LINK_TARGETS,
            policy=POLICY,
            actor=ACTOR_CONTEXT,
        )


def test_publish_rejects_missing_revision() -> None:
    uow = FakeContentUnitOfWork()
    created = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    with pytest.raises(RevisionNotFoundError):
        publish_content(
            PublishContent(
                content_id=created.content_id,
                revision_id=RevisionId(UUID(int=404)),
                expected_version=created.content_version,
                actor_id=ACTOR,
            ),
            uow=uow,
            clock=FrozenClock(),
            ids=SequenceIds(3),
            content_types=CONTENT_TYPES,
            policy=POLICY,
            actor=ACTOR_CONTEXT,
        )


def test_republishing_same_revision_does_not_emit_duplicate_event() -> None:
    uow = FakeContentUnitOfWork()
    created = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )
    first = publish_content(
        PublishContent(
            content_id=created.content_id,
            revision_id=created.revision_id,
            expected_version=created.content_version,
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(3),
        content_types=CONTENT_TYPES,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    second = publish_content(
        PublishContent(
            content_id=created.content_id,
            revision_id=created.revision_id,
            expected_version=first.content_version,
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(4),
        content_types=CONTENT_TYPES,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    assert second.content_version == first.content_version
    assert len(uow.events.items) == 1


def test_create_content_persists_content_type_normalized_snapshot() -> None:
    class NormalizingContentTypes(FakeContentTypes):
        def prepare_for_write(
            self,
            *,
            type_key: ContentTypeKey,
            schema_version: int,
            data: Mapping[str, object],
        ) -> PreparedContentData:
            del type_key, schema_version, data
            return PreparedContentData(
                schema_version=7,
                data=RevisionData.from_mapping({"normalized": True}),
            )

    uow = FakeContentUnitOfWork()
    result = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={"legacy": "value"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(10, 11),
        content_types=NormalizingContentTypes(),
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    revision = uow.revisions.get(result.revision_id)
    assert revision is not None
    assert revision.schema_version == 7
    assert revision.data.as_dict() == {"normalized": True}


def test_archive_content_blocks_further_edits_and_publishes() -> None:
    uow = FakeContentUnitOfWork()
    created = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={"title": "First"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    archived = archive_content(
        ArchiveContent(
            content_id=created.content_id,
            expected_version=created.content_version,
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(3),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    assert archived.is_archived is True
    assert archived.content_version == 2
    assert len(uow.events.items) == 1
    assert uow.events.items[0].type == "strata.content.archived"

    with pytest.raises(ContentArchivedError):
        create_revision(
            CreateRevision(
                content_id=created.content_id,
                expected_version=archived.content_version,
                schema_version=1,
                data={"title": "Edit while archived"},
                actor_id=ACTOR,
            ),
            uow=uow,
            clock=FrozenClock(),
            ids=SequenceIds(4),
            content_types=CONTENT_TYPES,
            link_targets=LINK_TARGETS,
            policy=POLICY,
            actor=ACTOR_CONTEXT,
        )

    with pytest.raises(ContentArchivedError):
        publish_content(
            PublishContent(
                content_id=created.content_id,
                revision_id=created.revision_id,
                expected_version=archived.content_version,
                actor_id=ACTOR,
            ),
            uow=uow,
            clock=FrozenClock(),
            ids=SequenceIds(5),
            content_types=CONTENT_TYPES,
            policy=POLICY,
            actor=ACTOR_CONTEXT,
        )


def test_archiving_twice_does_not_emit_duplicate_event() -> None:
    uow = FakeContentUnitOfWork()
    created = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )
    first = archive_content(
        ArchiveContent(
            content_id=created.content_id,
            expected_version=created.content_version,
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(3),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    second = archive_content(
        ArchiveContent(
            content_id=created.content_id,
            expected_version=first.content_version,
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(4),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    assert second.content_version == first.content_version
    assert len(uow.events.items) == 1


def test_restore_content_allows_edits_again() -> None:
    uow = FakeContentUnitOfWork()
    created = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={"title": "First"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )
    archived = archive_content(
        ArchiveContent(
            content_id=created.content_id,
            expected_version=created.content_version,
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(3),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    restored = restore_content(
        RestoreContent(
            content_id=created.content_id,
            expected_version=archived.content_version,
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(4),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    assert restored.is_archived is False
    revised = create_revision(
        CreateRevision(
            content_id=created.content_id,
            expected_version=restored.content_version,
            schema_version=1,
            data={"title": "Edit after restore"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(5),
        content_types=CONTENT_TYPES,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )
    assert revised.revision_number == 2


def test_archive_rejects_missing_content() -> None:
    uow = FakeContentUnitOfWork()

    with pytest.raises(ContentNotFoundError):
        archive_content(
            ArchiveContent(
                content_id=ContentId(UUID(int=404)),
                expected_version=0,
                actor_id=ACTOR,
            ),
            uow=uow,
            clock=FrozenClock(),
            ids=SequenceIds(1),
            policy=POLICY,
            actor=ACTOR_CONTEXT,
        )


def _created_with_revision(
    uow: FakeContentUnitOfWork, content_types: FakeContentTypes
) -> tuple[ContentId, RevisionId]:
    created = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={"title": "First"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=content_types,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )
    return created.content_id, created.revision_id


def test_create_revision_succeeds_when_readiness_has_issues() -> None:
    content_types = FakeContentTypes()
    content_types.readiness_problems = (
        ContentDataProblem(code="fake_not_ready", message="Not ready."),
    )
    uow = FakeContentUnitOfWork()
    content_id, _revision_id = _created_with_revision(uow, content_types)

    second = create_revision(
        CreateRevision(
            content_id=content_id,
            expected_version=1,
            schema_version=1,
            data={"title": "Second"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(3),
        content_types=content_types,
        link_targets=LINK_TARGETS,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    assert second.revision_number == 2
    assert content_types.readiness_calls == 0


def test_publish_rejects_with_content_not_ready_and_does_not_publish() -> None:
    content_types = FakeContentTypes()
    content_types.readiness_problems = (
        ContentDataProblem(
            code="fake_not_ready", message="Not ready.", path="$.body[0].data.text"
        ),
    )
    uow = FakeContentUnitOfWork()
    content_id, revision_id = _created_with_revision(uow, content_types)

    with pytest.raises(ContentNotReadyError) as raised:
        publish_content(
            PublishContent(
                content_id=content_id,
                revision_id=revision_id,
                expected_version=1,
                actor_id=ACTOR,
            ),
            uow=uow,
            clock=FrozenClock(),
            ids=SequenceIds(9),
            content_types=content_types,
            policy=POLICY,
            actor=ACTOR_CONTEXT,
        )

    assert raised.value.type_key == ContentTypeKey("tests.article")
    assert raised.value.problems[0].code == "fake_not_ready"
    assert uow.events.items == []
    persisted = uow.contents.get(content_id)
    assert persisted is not None
    assert persisted.published_revision_id is None
    assert uow.committed is False


SPECIES = ContentTypeKey("tests.species")
TARGET_ID = ContentId(UUID(int=99))
BLOB_ID = UUID(int=77)


def _reference(
    target: UUID, *, location: str = "species", path: str = "$.species"
) -> ContentLink:
    return ContentLink(
        kind="reference",
        location=location,
        target=target,
        allowed_types=frozenset({SPECIES}),
        path=path,
    )


def _blob(target: UUID) -> ContentLink:
    return ContentLink(
        kind="blob",
        location="cover",
        target=target,
        allowed_types=frozenset(),
        path="$.cover",
    )


def _revise(
    uow: FakeContentUnitOfWork,
    content_id: ContentId,
    *,
    content_types: FakeContentTypes,
    link_targets: FakeLinkTargets,
) -> None:
    create_revision(
        CreateRevision(
            content_id=content_id,
            expected_version=1,
            schema_version=1,
            data={"title": "Second"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(3),
        content_types=content_types,
        link_targets=link_targets,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )


def _rejected_revision(
    links: ContentLinks, link_targets: FakeLinkTargets
) -> tuple[FakeContentUnitOfWork, list[tuple[str, str]]]:
    uow = FakeContentUnitOfWork()
    content_id, _revision_id = _created_with_revision(uow, CONTENT_TYPES)
    with pytest.raises(InvalidContentDataError) as raised:
        _revise(
            uow,
            content_id,
            content_types=LinkingContentTypes(links),
            link_targets=link_targets,
        )
    return uow, [(p.code, p.path) for p in raised.value.problems]


def test_create_revision_rejects_missing_reference_target() -> None:
    uow, problems = _rejected_revision(
        ContentLinks(references=(_reference(TARGET_ID),)), FakeLinkTargets()
    )

    assert problems == [("reference_not_found", "$.species")]
    assert len(uow.revisions.items) == 1
    assert len(uow.projections.revisions) == 1
    assert uow.committed is False


def test_create_revision_rejects_archived_target() -> None:
    targets = FakeLinkTargets(
        {TARGET_ID: LinkTarget(content_id=TARGET_ID, type_key=SPECIES, archived=True)}
    )

    _uow, problems = _rejected_revision(
        ContentLinks(references=(_reference(TARGET_ID),)), targets
    )

    assert problems == [("reference_archived", "$.species")]


def test_create_revision_rejects_wrong_target_type() -> None:
    targets = FakeLinkTargets(
        {
            TARGET_ID: LinkTarget(
                content_id=TARGET_ID,
                type_key=ContentTypeKey("tests.article"),
                archived=False,
            )
        }
    )

    _uow, problems = _rejected_revision(
        ContentLinks(
            references=(
                _reference(
                    TARGET_ID,
                    location=f"body/blocks/{UUID(int=5)}/data/page",
                    path="$.body[0].data.page",
                ),
            )
        ),
        targets,
    )

    assert problems == [("reference_type_not_allowed", "$.body[0].data.page")]


def test_create_revision_rejects_missing_blob() -> None:
    _uow, problems = _rejected_revision(
        ContentLinks(blobs=(_blob(BLOB_ID),)), FakeLinkTargets()
    )

    assert problems == [("blob_not_found", "$.cover")]


def test_create_revision_reports_every_link_problem() -> None:
    _uow, problems = _rejected_revision(
        ContentLinks(
            references=(_reference(TARGET_ID),),
            blobs=(_blob(BLOB_ID),),
        ),
        FakeLinkTargets(),
    )

    assert problems == [
        ("reference_not_found", "$.species"),
        ("blob_not_found", "$.cover"),
    ]


def test_create_revision_allows_unpublished_target() -> None:
    uow = FakeContentUnitOfWork()
    content_id, _revision_id = _created_with_revision(uow, CONTENT_TYPES)
    targets = FakeLinkTargets(
        {TARGET_ID: LinkTarget(content_id=TARGET_ID, type_key=SPECIES, archived=False)},
        blobs=frozenset({BLOB_ID}),
    )
    links = ContentLinks(references=(_reference(TARGET_ID),), blobs=(_blob(BLOB_ID),))

    _revise(
        uow,
        content_id,
        content_types=LinkingContentTypes(links),
        link_targets=targets,
    )

    assert len(uow.revisions.items) == 2
    assert uow.projections.revisions[-1].links == links


def test_revision_may_reference_its_own_content() -> None:
    uow = FakeContentUnitOfWork()
    content_id, _revision_id = _created_with_revision(uow, CONTENT_TYPES)
    link = ContentLink(
        kind="reference",
        location="related",
        target=content_id,
        allowed_types=frozenset({ContentTypeKey("tests.article")}),
        path="$.related",
    )

    _revise(
        uow,
        content_id,
        content_types=LinkingContentTypes(ContentLinks(references=(link,))),
        link_targets=UowLinkTargets(uow),
    )

    assert len(uow.revisions.items) == 2


def test_create_content_rejects_reference_to_its_own_new_id() -> None:
    uow = FakeContentUnitOfWork()
    own_id = UUID(int=1)  # first id handed out by SequenceIds(1, 2)
    link = ContentLink(
        kind="reference",
        location="related",
        target=own_id,
        allowed_types=frozenset({ContentTypeKey("tests.article")}),
        path="$.related",
    )

    with pytest.raises(InvalidContentDataError) as raised:
        create_content(
            CreateContent(
                type_key=ContentTypeKey("tests.article"),
                schema_version=1,
                data={"title": "First"},
                actor_id=ACTOR,
            ),
            uow=uow,
            clock=FrozenClock(),
            ids=SequenceIds(1, 2),
            content_types=LinkingContentTypes(ContentLinks(references=(link,))),
            link_targets=UowLinkTargets(uow),
            policy=POLICY,
            actor=ACTOR_CONTEXT,
        )

    assert [(p.code, p.path) for p in raised.value.problems] == [
        ("reference_not_found", "$.related")
    ]
    assert uow.contents.items == {}
    assert uow.projections.revisions == []


def test_create_revision_records_projection_with_title_and_links() -> None:
    uow = FakeContentUnitOfWork()
    targets = FakeLinkTargets(
        {TARGET_ID: LinkTarget(content_id=TARGET_ID, type_key=SPECIES, archived=False)},
        blobs=frozenset({BLOB_ID}),
    )
    links = ContentLinks(references=(_reference(TARGET_ID),), blobs=(_blob(BLOB_ID),))
    content_types = LinkingContentTypes(links, title="Hello")
    created = create_content(
        CreateContent(
            type_key=ContentTypeKey("tests.article"),
            schema_version=1,
            data={"title": "Hello"},
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(1, 2),
        content_types=content_types,
        link_targets=targets,
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )

    _revise(
        uow,
        created.content_id,
        content_types=content_types,
        link_targets=targets,
    )

    assert uow.projections.revisions == [
        RecordedRevision(
            content_id=created.content_id,
            revision_id=created.revision_id,
            type_key=ContentTypeKey("tests.article"),
            title="Hello",
            links=links,
        ),
        RecordedRevision(
            content_id=created.content_id,
            revision_id=RevisionId(UUID(int=3)),
            type_key=ContentTypeKey("tests.article"),
            title="Hello",
            links=links,
        ),
    ]


def test_archive_marks_title_archived() -> None:
    uow = FakeContentUnitOfWork()
    content_id, _revision_id = _created_with_revision(uow, CONTENT_TYPES)

    archived = archive_content(
        ArchiveContent(content_id=content_id, expected_version=1, actor_id=ACTOR),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(3),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )
    assert uow.projections.archived == {content_id: True}

    restore_content(
        RestoreContent(
            content_id=content_id,
            expected_version=archived.content_version,
            actor_id=ACTOR,
        ),
        uow=uow,
        clock=FrozenClock(),
        ids=SequenceIds(4),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )
    assert uow.projections.archived == {content_id: False}
