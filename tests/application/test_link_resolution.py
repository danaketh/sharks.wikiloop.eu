from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pytest_django import DjangoAssertNumQueries

from strata_cms.application.content.commands import (
    ArchiveContent,
    CreateContent,
    PublishContent,
)
from strata_cms.application.content.delivery import get_published_content
from strata_cms.application.content.links import LinkResolver
from strata_cms.application.content.use_cases import (
    archive_content,
    create_content,
    publish_content,
)
from strata_cms.application.errors import (
    ContentDataProblem,
    ContentTypeUnavailableError,
)
from strata_cms.application.ports.authorization import ContentAction, ContentActor
from strata_cms.application.ports.content_delivery import PublishedRevision
from strata_cms.application.ports.content_types import (
    ContentLinks,
    PreparedContentData,
)
from strata_cms.domain.media import MediaBlob
from strata_cms.domain.revision import Revision, RevisionData
from strata_cms.domain.value_objects import (
    ActorId,
    ContentId,
    ContentTypeKey,
    RevisionId,
)
from strata_cms.infrastructure.persistence.django.media import (
    DjangoMediaBlobRepository,
)
from strata_cms.infrastructure.persistence.django.models import MediaBlobRecord
from strata_cms.infrastructure.persistence.django.projections import (
    DjangoLinkTargetQueries,
)
from strata_cms.infrastructure.persistence.django.queries import (
    DjangoContentDeliveryQueries,
)
from strata_cms.infrastructure.persistence.django.routing import (
    DjangoRoutePathProjection,
)
from strata_cms.infrastructure.persistence.django.unit_of_work import (
    DjangoContentUnitOfWork,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
ACTOR = ActorId("user:7")
SPECIES = ContentTypeKey("tests.species")
MEDIA = ContentTypeKey("tests.media")
REMOVED = ContentTypeKey("tests.removed")
INSTALLED = frozenset({SPECIES, MEDIA})


class FakeContentTypes:
    """Delivery data is the stored data; ``tests.media`` embeds in references."""

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
        self._require(revision.content_type)

    def publish_issues(self, revision: Revision) -> tuple[ContentDataProblem, ...]:
        del revision
        return ()

    def delivery_data(self, revision: Revision) -> dict[str, object]:
        self._require(revision.content_type)
        return revision.data.as_dict()

    def extract_links(
        self, *, type_key: ContentTypeKey, data: RevisionData
    ) -> ContentLinks:
        del type_key, data
        return ContentLinks()

    def summary_title(self, revision: Revision) -> str | None:
        self._require(revision.content_type)
        title = revision.data.as_dict().get("title")
        return title if isinstance(title, str) else None

    def embeds_in_references(self, type_key: ContentTypeKey) -> bool:
        self._require(type_key)
        return type_key == MEDIA

    def revision_links(self, revision: Revision) -> ContentLinks:
        del revision
        return ContentLinks()

    def blob_ids(self, revision: Revision) -> frozenset[UUID]:
        del revision
        return frozenset()

    @staticmethod
    def _require(type_key: ContentTypeKey) -> None:
        if type_key not in INSTALLED:
            raise ContentTypeUnavailableError(type_key)


class FakePublishedLinkQueries:
    def __init__(self) -> None:
        self.rows: dict[ContentId, PublishedRevision] = {}
        self.calls: list[frozenset[ContentId]] = []

    def publish(
        self, number: int, type_key: ContentTypeKey, data: dict[str, object]
    ) -> ContentId:
        content_id = ContentId(UUID(int=number))
        self.rows[content_id] = PublishedRevision(
            content_id=content_id,
            type_key=type_key,
            revision=Revision(
                id=RevisionId(UUID(int=number + 10_000)),
                content_id=content_id,
                number=1,
                content_type=type_key,
                schema_version=1,
                data=RevisionData.from_mapping(data),
                created_at=NOW,
                created_by=ACTOR,
            ),
            published_at=NOW,
        )
        return content_id

    def published_revisions(
        self, ids: frozenset[ContentId]
    ) -> Mapping[ContentId, PublishedRevision]:
        self.calls.append(ids)
        return {key: row for key, row in self.rows.items() if key in ids}


class FakeBlobs:
    def __init__(self) -> None:
        self.blobs: dict[UUID, MediaBlob] = {}
        self.calls: list[frozenset[UUID]] = []

    def add_blob(self, number: int) -> UUID:
        blob_id = UUID(int=number)
        self.blobs[blob_id] = MediaBlob(
            id=blob_id,
            storage_key=f"media/{blob_id}.jpg",
            sha256="0" * 64,
            content_type="image/jpeg",
            size_bytes=1234,
            width=640,
            height=480,
            created_at=NOW,
            created_by=ACTOR,
        )
        return blob_id

    def add(self, blob: MediaBlob) -> None:
        self.blobs[blob.id] = blob

    def get_many(self, ids: frozenset[UUID]) -> Mapping[UUID, MediaBlob]:
        self.calls.append(ids)
        return {key: blob for key, blob in self.blobs.items() if key in ids}

    def list_orphans(
        self, *, created_before: datetime, limit: int
    ) -> tuple[MediaBlob, ...]:
        del created_before, limit
        return ()

    def delete(self, blob_id: UUID) -> None:
        self.blobs.pop(blob_id, None)


class FakeFiles:
    def save(self, key: str, data: bytes) -> None:
        del key, data

    def url(self, key: str) -> str:
        return f"https://cdn.example.invalid/{key}"

    def delete(self, key: str) -> None:
        del key


class FakeRoutes:
    def __init__(self) -> None:
        self.paths: dict[ContentId, str] = {}
        self.calls: list[frozenset[ContentId]] = []

    def set_path(self, content_id: ContentId, path: str) -> None:
        self.paths[content_id] = path

    def remove(self, content_id: ContentId) -> None:
        self.paths.pop(content_id, None)

    def resolve(self, path: str) -> ContentId | None:
        del path
        return None

    def paths_for(self, ids: frozenset[ContentId]) -> Mapping[ContentId, str]:
        self.calls.append(ids)
        return {key: path for key, path in self.paths.items() if key in ids}


class Harness:
    def __init__(self) -> None:
        self.queries = FakePublishedLinkQueries()
        self.blobs = FakeBlobs()
        self.routes = FakeRoutes()
        self.resolver = LinkResolver(
            queries=self.queries,
            content_types=FakeContentTypes(),
            blobs=self.blobs,
            files=FakeFiles(),
            routes=self.routes,
        )


def _ref(content_id: UUID) -> dict[str, object]:
    return {"$ref": str(content_id)}


def test_published_target_resolves_to_summary_with_title_and_path() -> None:
    harness = Harness()
    target = harness.queries.publish(1, SPECIES, {"title": "Great white"})
    harness.routes.set_path(target, "species/great-white")

    resolved = harness.resolver.resolve({"related": _ref(target), "n": 3})

    assert resolved == {
        "related": {
            "id": str(target),
            "type": "tests.species",
            "title": "Great white",
            "path": "species/great-white",
        },
        "n": 3,
    }


def test_target_without_route_has_null_path() -> None:
    harness = Harness()
    target = harness.queries.publish(1, SPECIES, {"title": "Mako"})

    resolved = harness.resolver.resolve({"related": _ref(target)})

    assert resolved["related"] == {
        "id": str(target),
        "type": "tests.species",
        "title": "Mako",
        "path": None,
    }


def test_unpublished_target_resolves_to_null() -> None:
    harness = Harness()

    resolved = harness.resolver.resolve({"related": _ref(UUID(int=99))})

    assert resolved == {"related": None}


def test_missing_target_list_item_removed() -> None:
    harness = Harness()
    kept = harness.queries.publish(1, SPECIES, {"title": "Kept"})

    resolved = harness.resolver.resolve(
        {"items": [_ref(UUID(int=50)), _ref(kept), _ref(UUID(int=51))]}
    )

    items = resolved["items"]
    assert isinstance(items, list)
    assert [item["id"] for item in items if isinstance(item, dict)] == [str(kept)]
    assert len(items) == 1


def test_list_keeps_non_link_null_items() -> None:
    harness = Harness()

    assert harness.resolver.resolve({"items": [None, 1]}) == {"items": [None, 1]}


def test_uninstalled_target_type_resolves_to_null() -> None:
    harness = Harness()
    gone = harness.queries.publish(1, REMOVED, {"title": "Orphan"})

    resolved = harness.resolver.resolve({"single": _ref(gone), "many": [_ref(gone)]})

    assert resolved == {"single": None, "many": []}


def test_blob_resolves_to_descriptor_or_null() -> None:
    harness = Harness()
    blob_id = harness.blobs.add_blob(7)

    resolved = harness.resolver.resolve(
        {"cover": {"$blob": str(blob_id)}, "missing": {"$blob": str(UUID(int=8))}}
    )

    assert resolved == {
        "cover": {
            "url": f"https://cdn.example.invalid/media/{blob_id}.jpg",
            "content_type": "image/jpeg",
            "width": 640,
            "height": 480,
            "size_bytes": 1234,
        },
        "missing": None,
    }


def test_embedded_target_includes_resolved_blob() -> None:
    harness = Harness()
    blob_id = harness.blobs.add_blob(7)
    media = harness.queries.publish(
        1, MEDIA, {"title": "Fin", "file": {"$blob": str(blob_id)}}
    )

    resolved = harness.resolver.resolve(
        {"cover": {"$blob": str(blob_id)}, "image": _ref(media)}
    )

    image = resolved["image"]
    assert isinstance(image, dict)
    assert image["title"] == "Fin"
    data = image["data"]
    assert isinstance(data, dict)
    file = data["file"]
    assert isinstance(file, dict)
    assert str(file["url"]).endswith(".jpg")
    assert data["title"] == "Fin"
    # Blobs from the document and from embedded target data share one batch.
    assert harness.blobs.calls == [frozenset({blob_id})]


def test_non_embedding_target_has_no_data() -> None:
    harness = Harness()
    target = harness.queries.publish(1, SPECIES, {"title": "Tiger"})

    resolved = harness.resolver.resolve({"related": _ref(target)})

    related = resolved["related"]
    assert isinstance(related, dict)
    assert "data" not in related


def test_nested_refs_in_embedded_data_not_expanded() -> None:
    harness = Harness()
    nested = harness.queries.publish(2, SPECIES, {"title": "Nested secret"})
    media = harness.queries.publish(
        1,
        MEDIA,
        {"title": "Fin", "subject": _ref(nested), "also": [_ref(nested)]},
    )

    resolved = harness.resolver.resolve({"image": _ref(media)})

    image = resolved["image"]
    assert isinstance(image, dict)
    assert image["data"] == {
        "title": "Fin",
        "subject": {"id": str(nested)},
        "also": [{"id": str(nested)}],
    }
    # Only the top-level target was loaded; the nested ref was not.
    assert harness.queries.calls == [frozenset({media})]


def test_one_batched_call_per_kind() -> None:
    harness = Harness()
    targets = [
        harness.queries.publish(number, SPECIES, {"title": f"T{number}"})
        for number in range(1, 6)
    ]
    blob_ids = [harness.blobs.add_blob(100 + number) for number in range(3)]

    harness.resolver.resolve(
        {
            "refs": [_ref(target) for target in targets],
            "blobs": [{"$blob": str(blob_id)} for blob_id in blob_ids],
            "deep": {"x": [{"y": _ref(targets[0])}]},
        }
    )

    assert harness.queries.calls == [frozenset(targets)]
    assert harness.blobs.calls == [frozenset(blob_ids)]
    assert harness.routes.calls == [frozenset(targets)]


def test_document_without_links_makes_no_calls() -> None:
    harness = Harness()

    assert harness.resolver.resolve({"title": "Plain"}) == {"title": "Plain"}
    assert harness.queries.calls == []
    assert harness.blobs.calls == []
    assert harness.routes.calls == []


def test_malformed_link_values_resolve_to_null() -> None:
    harness = Harness()

    resolved = harness.resolver.resolve(
        {"bad_ref": {"$ref": "not-a-uuid"}, "bad_blob": {"$blob": 5}}
    )

    assert resolved == {"bad_ref": None, "bad_blob": None}


# --- database-backed query budget ---------------------------------------------


class AllowAllPolicy:
    def authorize(self, actor: ContentActor, action: ContentAction) -> None:
        del actor, action


class FixedClock:
    def now(self) -> datetime:
        return NOW


class SequenceIds:
    def __init__(self, start: int) -> None:
        self._next = start

    def new_uuid(self) -> UUID:
        self._next += 1
        return UUID(int=self._next)


_ACTOR_CONTEXT = ContentActor(id=ACTOR, is_staff=True, permissions=frozenset())


def _publish_new(data: dict[str, object], *, id_start: int) -> tuple[ContentId, int]:
    content_types = FakeContentTypes()
    created = create_content(
        CreateContent(type_key=SPECIES, schema_version=1, data=data, actor_id=ACTOR),
        uow=DjangoContentUnitOfWork(clock=FixedClock()),
        clock=FixedClock(),
        ids=SequenceIds(id_start),
        content_types=content_types,
        link_targets=DjangoLinkTargetQueries(),
        policy=AllowAllPolicy(),
        actor=_ACTOR_CONTEXT,
    )
    published = publish_content(
        PublishContent(
            content_id=created.content_id,
            revision_id=created.revision_id,
            expected_version=created.content_version,
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=FixedClock()),
        clock=FixedClock(),
        ids=SequenceIds(id_start + 50),
        content_types=content_types,
        policy=AllowAllPolicy(),
        actor=_ACTOR_CONTEXT,
    )
    return created.content_id, published.content_version


def _create_published(data: dict[str, object], *, id_start: int) -> ContentId:
    return _publish_new(data, id_start=id_start)[0]


def _django_resolver() -> LinkResolver:
    return LinkResolver(
        queries=DjangoContentDeliveryQueries(),
        content_types=FakeContentTypes(),
        blobs=DjangoMediaBlobRepository(),
        files=FakeFiles(),
        routes=DjangoRoutePathProjection(),
    )


@pytest.mark.django_db
def test_archived_target_list_item_removed() -> None:
    kept = _create_published({"title": "Kept shark"}, id_start=1_000)
    archived, version = _publish_new({"title": "Archived shark"}, id_start=2_000)
    archive_content(
        ArchiveContent(content_id=archived, expected_version=version, actor_id=ACTOR),
        uow=DjangoContentUnitOfWork(clock=FixedClock()),
        clock=FixedClock(),
        ids=SequenceIds(3_000),
        policy=AllowAllPolicy(),
        actor=_ACTOR_CONTEXT,
    )

    assert archived not in DjangoContentDeliveryQueries().published_revisions(
        frozenset({archived, kept})
    )
    resolved = _django_resolver().resolve(
        {"single": _ref(archived), "items": [_ref(archived), _ref(kept)]}
    )

    assert resolved["single"] is None
    items = resolved["items"]
    assert isinstance(items, list)
    assert [item["id"] for item in items if isinstance(item, dict)] == [str(kept)]
    assert "Archived shark" not in repr(resolved)


@pytest.mark.django_db
def test_fifty_references_constant_queries(
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    targets = [
        _create_published({"title": f"Shark {n}"}, id_start=1_000 + n * 100)
        for n in range(50)
    ]
    blob_id = UUID(int=77)
    MediaBlobRecord.objects.create(
        id=blob_id,
        storage_key=f"media/{blob_id}.jpg",
        sha256="0" * 64,
        content_type="image/jpeg",
        size_bytes=10,
        width=1,
        height=1,
        created_at=NOW,
        created_by=ACTOR,
    )
    DjangoRoutePathProjection().set_path(targets[0], "sharks/zero")
    source = _create_published(
        {
            "title": "Index",
            "refs": [_ref(target) for target in targets],
            "cover": {"$blob": str(blob_id)},
        },
        id_start=900_000,
    )
    content_types = FakeContentTypes()
    queries = DjangoContentDeliveryQueries()
    resolver = LinkResolver(
        queries=queries,
        content_types=content_types,
        blobs=DjangoMediaBlobRepository(),
        files=FakeFiles(),
        routes=DjangoRoutePathProjection(),
    )

    with django_assert_max_num_queries(6):
        published = get_published_content(
            source, queries=queries, content_types=content_types, links=resolver
        )

    assert published is not None
    refs = published.data["refs"]
    assert isinstance(refs, list)
    assert len(refs) == 50
    assert refs[0] == {
        "id": str(targets[0]),
        "type": "tests.species",
        "title": "Shark 0",
        "path": "sharks/zero",
    }
    assert isinstance(refs[1], dict)
    assert refs[1]["path"] is None
    assert published.data["cover"] is not None
