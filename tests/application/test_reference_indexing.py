from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from strata_cms.application.content.reference_indexing import (
    reindex_references_from_event,
)
from strata_cms.application.errors import ContentDataProblem
from strata_cms.application.ports.content_delivery import PublishedRevision
from strata_cms.application.ports.content_types import (
    ContentLink,
    ContentLinks,
    PreparedContentData,
)
from strata_cms.application.ports.messaging import MessageEnvelope
from strata_cms.domain.revision import Revision, RevisionData
from strata_cms.domain.value_objects import (
    ActorId,
    ContentId,
    ContentTypeKey,
    RevisionId,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
SOURCE = ContentId(UUID(int=1))
TARGET_A = UUID(int=2)
TARGET_B = UUID(int=3)
PROFILE = ContentTypeKey("tests.profile")
SPECIES = ContentTypeKey("tests.species")


class FakeQueries:
    def __init__(self, published: PublishedRevision | None) -> None:
        self.published = published

    def get_published(self, content_id: ContentId) -> PublishedRevision | None:
        if self.published is None or self.published.content_id != content_id:
            return None
        return self.published

    def list_published(self, **kwargs: object) -> tuple[tuple[()], int]:
        del kwargs
        return (), 0


class RefsContentTypes:
    """Reference links come from the revision's ``refs`` UUID list."""

    def prepare_for_write(
        self,
        *,
        type_key: ContentTypeKey,
        schema_version: int,
        data: Mapping[str, object],
    ) -> PreparedContentData:
        raise NotImplementedError

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
        refs = data.as_dict().get("refs", [])
        assert isinstance(refs, list)
        return ContentLinks(
            references=tuple(
                ContentLink(
                    kind="reference",
                    location=f"refs/{index}",
                    target=UUID(str(value)),
                    allowed_types=frozenset({SPECIES}),
                    path=f"$.refs[{index}]",
                )
                for index, value in enumerate(refs)
            )
        )

    def summary_title(self, revision: Revision) -> str | None:
        del revision
        return None

    def embeds_in_references(self, type_key: ContentTypeKey) -> bool:
        del type_key
        return False

    def revision_links(self, revision: Revision) -> ContentLinks:
        return self.extract_links(type_key=revision.content_type, data=revision.data)

    def blob_ids(self, revision: Revision) -> frozenset[UUID]:
        del revision
        return frozenset()


class FakeIndex:
    def __init__(self) -> None:
        self.rows: dict[ContentId, tuple[RevisionId, tuple[ContentLink, ...]]] = {}
        self.calls: list[str] = []

    def replace(
        self,
        source_id: ContentId,
        revision_id: RevisionId,
        links: tuple[ContentLink, ...],
    ) -> None:
        self.calls.append("replace")
        self.rows[source_id] = (revision_id, links)

    def remove(self, source_id: ContentId) -> None:
        self.calls.append("remove")
        self.rows.pop(source_id, None)

    def referencing(
        self,
        target_id: ContentId,
        *,
        source_type: ContentTypeKey | None,
        limit: int,
        offset: int,
    ) -> tuple[tuple[ContentId, ...], int]:
        raise NotImplementedError


def _published(refs: list[UUID], *, revision_id: int = 10) -> PublishedRevision:
    revision = Revision(
        id=RevisionId(UUID(int=revision_id)),
        content_id=SOURCE,
        number=1,
        content_type=PROFILE,
        schema_version=1,
        data=RevisionData.from_mapping({"refs": [str(ref) for ref in refs]}),
        created_at=NOW,
        created_by=ActorId("user:1"),
    )
    return PublishedRevision(
        content_id=SOURCE, type_key=PROFILE, revision=revision, published_at=NOW
    )


def _event(event_type: str, content_id: ContentId = SOURCE) -> MessageEnvelope:
    return MessageEnvelope(
        id=uuid4(),
        type=event_type,
        version=1,
        payload={"content_id": str(content_id)},
        created_at=NOW,
    )


def _targets(index: FakeIndex) -> list[UUID]:
    return [link.target for link in index.rows[SOURCE][1]]


def test_publish_event_indexes_references() -> None:
    index = FakeIndex()

    reindex_references_from_event(
        _event("strata.content.published"),
        queries=FakeQueries(_published([TARGET_A, TARGET_B])),
        content_types=RefsContentTypes(),
        index=index,
    )

    assert index.rows[SOURCE][0] == RevisionId(UUID(int=10))
    assert _targets(index) == [TARGET_A, TARGET_B]
    assert [link.location for link in index.rows[SOURCE][1]] == [
        "refs/0",
        "refs/1",
    ]


def test_archive_event_removes_rows() -> None:
    index = FakeIndex()
    index.rows[SOURCE] = (RevisionId(UUID(int=10)), ())

    # Archived content is no longer published, so the query returns nothing.
    reindex_references_from_event(
        _event("strata.content.archived"),
        queries=FakeQueries(None),
        content_types=RefsContentTypes(),
        index=index,
    )

    assert SOURCE not in index.rows


def test_restore_event_reindexes_only_published_content() -> None:
    published_index = FakeIndex()
    unpublished_index = FakeIndex()

    reindex_references_from_event(
        _event("strata.content.restored"),
        queries=FakeQueries(_published([TARGET_A])),
        content_types=RefsContentTypes(),
        index=published_index,
    )
    reindex_references_from_event(
        _event("strata.content.restored"),
        queries=FakeQueries(None),
        content_types=RefsContentTypes(),
        index=unpublished_index,
    )

    assert _targets(published_index) == [TARGET_A]
    assert unpublished_index.rows == {}
    assert unpublished_index.calls == ["remove"]


def test_event_handling_is_idempotent() -> None:
    index = FakeIndex()
    queries = FakeQueries(_published([TARGET_A, TARGET_B]))

    for _ in range(3):
        reindex_references_from_event(
            _event("strata.content.published"),
            queries=queries,
            content_types=RefsContentTypes(),
            index=index,
        )
    snapshot = dict(index.rows)
    reindex_references_from_event(
        _event("strata.content.published"),
        queries=queries,
        content_types=RefsContentTypes(),
        index=index,
    )

    assert index.rows == snapshot
    assert _targets(index) == [TARGET_A, TARGET_B]


def test_redelivered_archive_event_after_republish_keeps_current_rows() -> None:
    """Handlers reconcile with current state, so redelivery order is harmless."""
    index = FakeIndex()

    reindex_references_from_event(
        _event("strata.content.archived"),
        queries=FakeQueries(_published([TARGET_A])),
        content_types=RefsContentTypes(),
        index=index,
    )

    assert _targets(index) == [TARGET_A]


def test_unrelated_and_malformed_events_are_ignored() -> None:
    index = FakeIndex()
    queries = FakeQueries(_published([TARGET_A]))

    reindex_references_from_event(
        _event("strata.route.moved"),
        queries=queries,
        content_types=RefsContentTypes(),
        index=index,
    )
    reindex_references_from_event(
        MessageEnvelope(
            id=uuid4(),
            type="strata.content.published",
            version=1,
            payload={"content_id": 5},
            created_at=NOW,
        ),
        queries=queries,
        content_types=RefsContentTypes(),
        index=index,
    )

    assert index.calls == []
