from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

import pytest

from strata_cms.application.content.commands import (
    ArchiveContent,
    CreateContent,
    CreateRevision,
    RestoreContent,
)
from strata_cms.application.content.results import ContentWriteResult
from strata_cms.application.content.use_cases import (
    archive_content,
    create_content,
    create_revision,
    restore_content,
)
from strata_cms.application.errors import ContentDataProblem, InvalidContentDataError
from strata_cms.application.ports.authorization import ContentAction, ContentActor
from strata_cms.application.ports.content_types import (
    ContentLink,
    ContentLinks,
    PreparedContentData,
)
from strata_cms.application.ports.links import LinkTarget
from strata_cms.domain.revision import Revision, RevisionData
from strata_cms.domain.value_objects import (
    ActorId,
    ContentId,
    ContentTypeKey,
    RevisionId,
)
from strata_cms.infrastructure.persistence.django.models import (
    ContentReferenceRecord,
    ContentTitleRecord,
    MediaBlobRecord,
    MediaBlobUsageRecord,
)
from strata_cms.infrastructure.persistence.django.projections import (
    DjangoLinkTargetQueries,
)
from strata_cms.infrastructure.persistence.django.unit_of_work import (
    DjangoContentUnitOfWork,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 16, 13, 0, tzinfo=UTC)
ACTOR = ActorId("user:42")
ACTOR_CONTEXT = ContentActor(id=ACTOR, is_staff=True, permissions=frozenset())
ARTICLE = ContentTypeKey("tests.article")
SPECIES = ContentTypeKey("tests.species")


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class SequenceIds:
    def __init__(self, start: int) -> None:
        self._next = start

    def new_uuid(self) -> UUID:
        self._next += 1
        return UUID(int=self._next)


class AllowAllPolicy:
    def authorize(self, actor: ContentActor, action: ContentAction) -> None:
        del actor, action


class DataDrivenContentTypes:
    """Links come from ``refs``/``blobs`` UUID lists; title from ``title``."""

    def prepare_for_write(
        self,
        *,
        type_key: ContentTypeKey,
        schema_version: int,
        data: Mapping[str, object],
    ) -> PreparedContentData:
        del type_key
        return PreparedContentData(
            schema_version=schema_version, data=RevisionData.from_mapping(data)
        )

    def validate_revision(self, revision: Revision) -> None:
        del revision

    def publish_issues(self, revision: Revision) -> tuple[ContentDataProblem, ...]:
        del revision
        return ()

    def delivery_data(self, revision: Revision) -> dict[str, object]:
        return revision.data.as_dict()

    def extract_links(
        self, *, type_key: ContentTypeKey, data: RevisionData
    ) -> ContentLinks:
        del type_key
        raw = data.as_dict()
        return ContentLinks(
            references=tuple(
                ContentLink(
                    kind="reference",
                    location=f"refs/{index}",
                    target=UUID(str(value)),
                    allowed_types=frozenset({ARTICLE, SPECIES}),
                    path=f"$.refs[{index}]",
                )
                for index, value in enumerate(_list(raw, "refs"))
            ),
            blobs=tuple(
                ContentLink(
                    kind="blob",
                    location=f"blobs/{index}",
                    target=UUID(str(value)),
                    allowed_types=frozenset(),
                    path=f"$.blobs[{index}]",
                )
                for index, value in enumerate(_list(raw, "blobs"))
            ),
        )

    def summary_title(self, revision: Revision) -> str | None:
        title = revision.data.as_dict().get("title")
        return title if isinstance(title, str) else None

    def embeds_in_references(self, type_key: ContentTypeKey) -> bool:
        del type_key
        return False

    def revision_links(self, revision: Revision) -> ContentLinks:
        return self.extract_links(type_key=revision.content_type, data=revision.data)

    def blob_ids(self, revision: Revision) -> frozenset[UUID]:
        return frozenset(link.target for link in self.revision_links(revision).blobs)


def _list(raw: Mapping[str, object], key: str) -> list[object]:
    value = raw.get(key, [])
    return value if isinstance(value, list) else []


CONTENT_TYPES = DataDrivenContentTypes()
POLICY = AllowAllPolicy()


def _create(
    data: dict[str, object],
    *,
    id_start: int,
    type_key: ContentTypeKey = ARTICLE,
) -> ContentWriteResult:
    return create_content(
        CreateContent(type_key=type_key, schema_version=1, data=data, actor_id=ACTOR),
        uow=DjangoContentUnitOfWork(clock=FixedClock()),
        clock=FixedClock(),
        ids=SequenceIds(id_start),
        content_types=CONTENT_TYPES,
        link_targets=DjangoLinkTargetQueries(),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )


def _revise(
    created: ContentWriteResult,
    data: dict[str, object],
    *,
    id_start: int,
    expected_version: int | None = None,
    clock: FixedClock | None = None,
) -> ContentWriteResult:
    return create_revision(
        CreateRevision(
            content_id=created.content_id,
            expected_version=(
                created.content_version
                if expected_version is None
                else expected_version
            ),
            schema_version=1,
            data=data,
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=clock or FixedClock()),
        clock=FixedClock(),
        ids=SequenceIds(id_start),
        content_types=CONTENT_TYPES,
        link_targets=DjangoLinkTargetQueries(),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )


def _blob(number: int) -> UUID:
    blob_id = UUID(int=number)
    MediaBlobRecord.objects.create(
        id=blob_id,
        storage_key=f"blobs/{blob_id}",
        sha256="0" * 64,
        content_type="image/png",
        size_bytes=10,
        width=1,
        height=1,
        created_at=NOW,
        created_by=ACTOR,
    )
    return blob_id


def _draft_rows(source: ContentId) -> list[tuple[str, UUID, str]]:
    return [
        (row.location, row.target_id, row.target_type)
        for row in ContentReferenceRecord.objects.filter(
            scope="draft", source_id=source
        ).order_by("location")
    ]


@pytest.mark.django_db
def test_draft_references_replaced_on_new_revision() -> None:
    species = _create({"title": "Shark"}, id_start=100, type_key=SPECIES)
    other = _create({"title": "Ray"}, id_start=200)
    source = _create({"title": "Src", "refs": [str(species.content_id)]}, id_start=300)

    assert _draft_rows(source.content_id) == [
        ("refs/0", species.content_id, "tests.species")
    ]
    row = ContentReferenceRecord.objects.get(source_id=source.content_id)
    assert row.source_revision_id == source.revision_id

    second = _revise(source, {"refs": [str(other.content_id)]}, id_start=400)

    assert _draft_rows(source.content_id) == [
        ("refs/0", other.content_id, "tests.article")
    ]
    row = ContentReferenceRecord.objects.get(source_id=source.content_id)
    assert row.source_revision_id == second.revision_id


@pytest.mark.django_db
def test_blob_usage_kept_for_all_revisions() -> None:
    first_blob, second_blob = _blob(11), _blob(12)
    created = _create({"blobs": [str(first_blob), str(first_blob)]}, id_start=100)
    second = _revise(created, {"blobs": [str(second_blob)]}, id_start=200)

    usage = set(
        MediaBlobUsageRecord.objects.values_list("blob_id", "content_id", "revision_id")
    )
    assert usage == {
        (first_blob, created.content_id, created.revision_id),
        (second_blob, created.content_id, second.revision_id),
    }


@pytest.mark.django_db
def test_projection_write_rolls_back_with_failed_uow() -> None:
    target = _create({"title": "Target"}, id_start=100)
    blob_id = _blob(11)
    links = ContentLinks(
        references=(
            ContentLink(
                kind="reference",
                location="refs/0",
                target=target.content_id,
                allowed_types=frozenset({ARTICLE}),
                path="$.refs[0]",
            ),
        ),
        blobs=(
            ContentLink(
                kind="blob",
                location="blobs/0",
                target=blob_id,
                allowed_types=frozenset(),
                path="$.blobs[0]",
            ),
        ),
    )
    source_id = ContentId(UUID(int=900))

    def write_then_fail() -> None:
        with DjangoContentUnitOfWork(clock=FixedClock()) as uow:
            uow.projections.record_revision(
                content_id=source_id,
                revision_id=RevisionId(UUID(int=901)),
                type_key=ARTICLE,
                title="Never",
                links=links,
            )
            uow.projections.set_archived(target.content_id, True)
            uow.commit()
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        write_then_fail()

    assert not ContentTitleRecord.objects.filter(pk=source_id).exists()
    assert not ContentReferenceRecord.objects.filter(source_id=source_id).exists()
    assert not MediaBlobUsageRecord.objects.filter(content_id=source_id).exists()
    assert ContentTitleRecord.objects.get(pk=target.content_id).archived is False


@pytest.mark.django_db
def test_title_projection_tracks_latest_draft_and_archival() -> None:
    created = _create({"title": "First"}, id_start=100)
    title = ContentTitleRecord.objects.get(pk=created.content_id)
    assert (title.type_key, title.title, title.archived) == (
        "tests.article",
        "First",
        False,
    )
    assert title.updated_at == NOW

    second = _revise(
        created, {"title": "Second"}, id_start=200, clock=FixedClock(LATER)
    )
    title = ContentTitleRecord.objects.get(pk=created.content_id)
    assert (title.title, title.updated_at) == ("Second", LATER)

    archived = archive_content(
        ArchiveContent(
            content_id=created.content_id,
            expected_version=second.content_version,
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=FixedClock()),
        clock=FixedClock(),
        ids=SequenceIds(300),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )
    assert ContentTitleRecord.objects.get(pk=created.content_id).archived is True

    restore_content(
        RestoreContent(
            content_id=created.content_id,
            expected_version=archived.content_version,
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=FixedClock()),
        clock=FixedClock(),
        ids=SequenceIds(400),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )
    assert ContentTitleRecord.objects.get(pk=created.content_id).archived is False


@pytest.mark.django_db
def test_link_target_queries_batch_lookup() -> None:
    live = _create({}, id_start=100, type_key=SPECIES)
    gone = _create({}, id_start=200)
    archive_content(
        ArchiveContent(
            content_id=gone.content_id,
            expected_version=gone.content_version,
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=FixedClock()),
        clock=FixedClock(),
        ids=SequenceIds(300),
        policy=POLICY,
        actor=ACTOR_CONTEXT,
    )
    blob_id = _blob(11)
    missing = ContentId(UUID(int=999))
    queries = DjangoLinkTargetQueries()

    targets = queries.content_targets(
        frozenset({live.content_id, gone.content_id, missing})
    )

    assert targets == {
        live.content_id: LinkTarget(
            content_id=live.content_id, type_key=SPECIES, archived=False
        ),
        gone.content_id: LinkTarget(
            content_id=gone.content_id, type_key=ARTICLE, archived=True
        ),
    }
    assert queries.existing_blobs(frozenset({blob_id, UUID(int=12)})) == frozenset(
        {blob_id}
    )
    assert queries.content_targets(frozenset()) == {}
    assert queries.existing_blobs(frozenset()) == frozenset()


@pytest.mark.django_db
def test_revision_referencing_its_own_content_is_accepted() -> None:
    created = _create({"title": "Self"}, id_start=100)

    _revise(created, {"refs": [str(created.content_id)]}, id_start=200)

    assert _draft_rows(created.content_id) == [
        ("refs/0", created.content_id, "tests.article")
    ]


@pytest.mark.django_db
def test_rejected_links_leave_no_revision_or_projection() -> None:
    created = _create({"title": "Src"}, id_start=100)

    with pytest.raises(InvalidContentDataError) as raised:
        _revise(
            created,
            {"title": "Bad", "refs": [str(UUID(int=999))], "blobs": [str(UUID(int=5))]},
            id_start=200,
        )

    assert [(p.code, p.path) for p in raised.value.problems] == [
        ("reference_not_found", "$.refs[0]"),
        ("blob_not_found", "$.blobs[0]"),
    ]
    assert ContentTitleRecord.objects.get(pk=created.content_id).title == "Src"
    assert not ContentReferenceRecord.objects.exists()


@pytest.mark.django_db
def test_long_summary_title_is_truncated_not_rejected() -> None:
    created = _create({"title": "x" * 10_000}, id_start=100)

    title = ContentTitleRecord.objects.get(pk=created.content_id).title
    assert title == "x" * 255


@pytest.mark.django_db
def test_title_projection_strips_whitespace_and_keeps_none() -> None:
    padded = _create({"title": "  Padded  "}, id_start=100)
    untitled = _create({}, id_start=200)

    assert ContentTitleRecord.objects.get(pk=padded.content_id).title == "Padded"
    assert ContentTitleRecord.objects.get(pk=untitled.content_id).title is None
