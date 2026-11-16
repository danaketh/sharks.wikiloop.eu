from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from strata_cms.api.delivery import views as delivery_views
from strata_cms.application.content.commands import (
    ArchiveContent,
    CreateContent,
    CreateRevision,
    PublishContent,
)
from strata_cms.application.content.results import ContentWriteResult
from strata_cms.application.content.use_cases import (
    archive_content,
    create_content,
    create_revision,
    publish_content,
)
from strata_cms.application.ports.authorization import ContentAction, ContentActor
from strata_cms.application.ports.search import SearchDocument
from strata_cms.config import services
from strata_cms.config.services import dispatch_content_event, get_search_backend
from strata_cms.domain.revision import RevisionData
from strata_cms.domain.value_objects import ActorId, ContentTypeKey
from strata_cms.examples.simple_article import EXAMPLE_CONTENT_TYPE
from strata_cms.infrastructure.clock.system import DjangoClock
from strata_cms.infrastructure.content_types import RegistryContentTypeService
from strata_cms.infrastructure.ids.uuid import Uuid4Generator
from strata_cms.infrastructure.persistence.django.outbox import DjangoOutboxQueue
from strata_cms.infrastructure.persistence.django.projections import (
    DjangoLinkTargetQueries,
)
from strata_cms.infrastructure.persistence.django.unit_of_work import (
    DjangoContentUnitOfWork,
)
from strata_cms.infrastructure.tasks.worker import run_worker_batch
from strata_cms.plugin_api import (
    ContentTypeDefinition,
    LinkPath,
    PluginDefinition,
    PluginKey,
    ReferenceFieldDefinition,
    StrataRegistryBuilder,
    ValidationIssue,
)

pytestmark = pytest.mark.django_db


def _staff_client() -> APIClient:
    user_model = get_user_model()
    user = user_model.objects.create_superuser(
        username="editor",
        email="editor@example.invalid",
        password="test-password",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_delivery_api_404s_for_unpublished_content() -> None:
    staff = _staff_client()
    create = staff.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "Draft only", "body": "Body"},
        },
        format="json",
    )
    assert create.status_code == 201
    content_id = create.json()["content_id"]

    anonymous = APIClient()
    response = anonymous.get(reverse("api:delivery:content-detail", args=[content_id]))

    assert response.status_code == 404


def test_delivery_api_returns_published_revision_to_anonymous_clients() -> None:
    staff = _staff_client()
    create = staff.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "Published", "body": "Body"},
        },
        format="json",
    )
    created = create.json()
    content_id = created["content_id"]

    staff.post(
        reverse("api:management:content-publish", args=[content_id]),
        {
            "expected_version": 1,
            "revision_id": created["revision_id"],
        },
        format="json",
    )

    revision = staff.post(
        reverse("api:management:content-revision-create", args=[content_id]),
        {
            "expected_version": 2,
            "schema_version": 2,
            "data": {"title": "Draft edit", "body": "Not yet public"},
        },
        format="json",
    )
    assert revision.status_code == 201

    anonymous = APIClient()
    response = anonymous.get(reverse("api:delivery:content-detail", args=[content_id]))

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["title"] == "Published"
    assert body["revision_id"] == created["revision_id"]


def test_delivery_api_404s_for_archived_content_even_if_published() -> None:
    staff = _staff_client()
    create = staff.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "Published then archived", "body": "Body"},
        },
        format="json",
    )
    created = create.json()
    content_id = created["content_id"]

    publish = staff.post(
        reverse("api:management:content-publish", args=[content_id]),
        {
            "expected_version": created["content_version"],
            "revision_id": created["revision_id"],
        },
        format="json",
    )
    assert publish.status_code == 200

    archive = staff.post(
        reverse("api:management:content-archive", args=[content_id]),
        {"expected_version": publish.json()["content_version"]},
        format="json",
    )
    assert archive.status_code == 200

    anonymous = APIClient()
    response = anonymous.get(reverse("api:delivery:content-detail", args=[content_id]))

    assert response.status_code == 404


def _publish(staff: APIClient, *, title: str) -> dict[str, object]:
    create = staff.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": title, "body": "Body"},
        },
        format="json",
    )
    created = create.json()
    staff.post(
        reverse("api:management:content-publish", args=[created["content_id"]]),
        {
            "expected_version": created["content_version"],
            "revision_id": created["revision_id"],
        },
        format="json",
    )
    assert isinstance(created, dict)
    return created


def test_delivery_list_returns_only_published_non_archived_content() -> None:
    staff = _staff_client()
    published = _publish(staff, title="Published item")
    draft = staff.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "Draft item", "body": "Body"},
        },
        format="json",
    ).json()
    archived = _publish(staff, title="Archived item")
    staff.post(
        reverse("api:management:content-archive", args=[archived["content_id"]]),
        {"expected_version": 2},
        format="json",
    )

    anonymous = APIClient()
    response = anonymous.get(reverse("api:delivery:content-list"))

    assert response.status_code == 200
    body = response.json()
    ids = {item["content_id"] for item in body["results"]}
    assert published["content_id"] in ids
    assert draft["content_id"] not in ids
    assert archived["content_id"] not in ids


def test_delivery_list_filters_by_type_and_paginates() -> None:
    staff = _staff_client()
    for i in range(3):
        _publish(staff, title=f"Item {i}")

    params: dict[str, str | int] = {
        "type": str(EXAMPLE_CONTENT_TYPE),
        "limit": 2,
        "offset": 0,
    }
    anonymous = APIClient()
    response = anonymous.get(reverse("api:delivery:content-list"), params)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["results"]) == 2


def test_delivery_list_rejects_unknown_type_key() -> None:
    anonymous = APIClient()

    response = anonymous.get(
        reverse("api:delivery:content-list"),
        {"type": "not a valid key"},
    )

    assert response.status_code == 400


def test_delivery_detail_serves_stale_cache_within_ttl() -> None:
    staff = _staff_client()
    created = _publish(staff, title="Cached title")
    content_id = created["content_id"]
    anonymous = APIClient()
    first = anonymous.get(reverse("api:delivery:content-detail", args=[content_id]))
    assert first.status_code == 200
    assert first.json()["data"]["title"] == "Cached title"

    staff.post(
        reverse("api:management:content-revision-create", args=[content_id]),
        {
            "expected_version": 2,
            "schema_version": 2,
            "data": {"title": "New title", "body": "Body"},
        },
        format="json",
    )
    staff.post(
        reverse("api:management:content-publish", args=[content_id]),
        {"expected_version": 3, "revision_id": created["revision_id"]},
        format="json",
    )

    second = anonymous.get(reverse("api:delivery:content-detail", args=[content_id]))
    assert second.status_code == 200
    assert second.json()["data"]["title"] == "Cached title"


def test_search_finds_indexed_content() -> None:
    get_search_backend().index(
        SearchDocument(
            id=UUID(int=1),
            text="Bengal cats are wonderful companions",
            fields={"type": str(EXAMPLE_CONTENT_TYPE)},
        )
    )

    anonymous = APIClient()
    response = anonymous.get(reverse("api:delivery:content-search"), {"q": "Bengal"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["results"][0]["content_id"] == str(UUID(int=1))


def test_search_requires_a_query_string() -> None:
    anonymous = APIClient()

    response = anonymous.get(reverse("api:delivery:content-search"))

    assert response.status_code == 400


def test_search_rejects_unknown_type_filter() -> None:
    anonymous = APIClient()

    response = anonymous.get(
        reverse("api:delivery:content-search"),
        {"q": "cats", "type": "not a valid key"},
    )

    assert response.status_code == 400


def test_publishing_content_does_not_appear_in_search_until_indexed() -> None:
    staff = _staff_client()
    _publish(staff, title="Not yet searchable")

    anonymous = APIClient()
    response = anonymous.get(
        reverse("api:delivery:content-search"), {"q": "searchable"}
    )

    assert response.status_code == 200
    assert response.json()["count"] == 0


# --- reference resolution ----------------------------------------------------

_SPECIES = ContentTypeKey("tests.species")
_PROFILE = ContentTypeKey("tests.profile")
_GUIDE = ContentTypeKey("tests.guide")
_ACTOR = ActorId("user:1")
_ACTOR_CONTEXT = ContentActor(id=_ACTOR, is_staff=True, permissions=frozenset())


class _DictSchema:
    value_type: type[dict[str, object]] = dict

    def deserialize(self, data: RevisionData) -> dict[str, object]:
        return data.as_dict()

    def serialize(self, value: dict[str, object]) -> RevisionData:
        return RevisionData.from_mapping(value)

    def validate(self, value: dict[str, object]) -> tuple[ValidationIssue, ...]:
        del value
        return ()


class _AllowAll:
    def authorize(self, actor: ContentActor, action: ContentAction) -> None:
        del actor, action


def _reference_service() -> RegistryContentTypeService:
    builder = StrataRegistryBuilder()
    plugin = PluginDefinition(
        key=PluginKey("tests.refs"), version="1.0.0", requires_strata=">=0.1,<0.2"
    )
    builder.register_plugin(plugin)
    registrar = builder.registrar_for(plugin.key)
    registrar.register_content_type(
        ContentTypeDefinition(
            key=_SPECIES,
            schema_version=1,
            schema=_DictSchema(),
            summary_title=lambda value: str(value.get("title", "")),
        )
    )
    registrar.register_content_type(
        ContentTypeDefinition(
            key=_PROFILE,
            schema_version=1,
            schema=_DictSchema(),
            reference_fields=(
                ReferenceFieldDefinition(
                    LinkPath.parse("related"), frozenset({_SPECIES})
                ),
                ReferenceFieldDefinition(
                    LinkPath.parse("items"), frozenset({_SPECIES}), many=True
                ),
            ),
        )
    )
    registrar.register_content_type(
        ContentTypeDefinition(
            key=_GUIDE,
            schema_version=1,
            schema=_DictSchema(),
            reference_fields=(
                ReferenceFieldDefinition(
                    LinkPath.parse("related"), frozenset({_SPECIES})
                ),
            ),
        )
    )
    return RegistryContentTypeService(builder.build(strata_version="0.1.0"))


def _write(
    content_types: RegistryContentTypeService,
    type_key: ContentTypeKey,
    data: dict[str, object],
) -> ContentWriteResult:
    return create_content(
        CreateContent(type_key=type_key, schema_version=1, data=data, actor_id=_ACTOR),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=content_types,
        link_targets=DjangoLinkTargetQueries(),
        policy=_AllowAll(),
        actor=_ACTOR_CONTEXT,
    )


def _publish_result(
    content_types: RegistryContentTypeService, written: ContentWriteResult
) -> int:
    published = publish_content(
        PublishContent(
            content_id=written.content_id,
            revision_id=written.revision_id,
            expected_version=written.content_version,
            actor_id=_ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=content_types,
        policy=_AllowAll(),
        actor=_ACTOR_CONTEXT,
    )
    return published.content_version


def test_delivery_endpoint_never_returns_draft_target_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_types = _reference_service()
    monkeypatch.setattr(services, "get_content_type_service", lambda: content_types)
    monkeypatch.setattr(
        delivery_views, "get_content_type_service", lambda: content_types
    )
    target = _write(content_types, _SPECIES, {"title": "Published shark"})
    target_version = _publish_result(content_types, target)
    create_revision(
        CreateRevision(
            content_id=target.content_id,
            expected_version=target_version,
            schema_version=1,
            data={"title": "Draft secret shark"},
            actor_id=_ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=content_types,
        link_targets=DjangoLinkTargetQueries(),
        policy=_AllowAll(),
        actor=_ACTOR_CONTEXT,
    )
    unpublished = _write(content_types, _SPECIES, {"title": "Never published"})
    source = _write(
        content_types,
        _PROFILE,
        {
            "related": {"$ref": str(target.content_id)},
            "other": "x",
        },
    )
    _publish_result(content_types, source)
    draft_only_source = _write(
        content_types,
        _PROFILE,
        {"related": {"$ref": str(unpublished.content_id)}},
    )
    _publish_result(content_types, draft_only_source)

    anonymous = APIClient()
    response = anonymous.get(
        reverse("api:delivery:content-detail", args=[source.content_id])
    )
    unpublished_response = anonymous.get(
        reverse("api:delivery:content-detail", args=[draft_only_source.content_id])
    )
    listing = anonymous.get(reverse("api:delivery:content-list"))

    assert response.status_code == 200
    assert response.json()["data"]["related"] == {
        "id": str(target.content_id),
        "type": str(_SPECIES),
        "title": "Published shark",
        "path": None,
    }
    assert "Draft secret" not in response.content.decode()
    assert unpublished_response.json()["data"]["related"] is None
    assert "Never published" not in unpublished_response.content.decode()
    by_id = {item["content_id"]: item for item in listing.json()["results"]}
    assert by_id[str(source.content_id)]["data"]["related"]["title"] == (
        "Published shark"
    )
    assert "Draft secret" not in listing.content.decode()


def test_delivery_endpoint_omits_archived_reference_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_types = _reference_service()
    monkeypatch.setattr(services, "get_content_type_service", lambda: content_types)
    monkeypatch.setattr(
        delivery_views, "get_content_type_service", lambda: content_types
    )
    kept = _write(content_types, _SPECIES, {"title": "Kept shark"})
    _publish_result(content_types, kept)
    archived = _write(content_types, _SPECIES, {"title": "Archived shark"})
    archived_version = _publish_result(content_types, archived)
    source = _write(
        content_types,
        _PROFILE,
        {
            "related": {"$ref": str(archived.content_id)},
            "items": [
                {"$ref": str(archived.content_id)},
                {"$ref": str(kept.content_id)},
            ],
        },
    )
    _publish_result(content_types, source)
    archive_content(
        ArchiveContent(
            content_id=archived.content_id,
            expected_version=archived_version,
            actor_id=_ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        policy=_AllowAll(),
        actor=_ACTOR_CONTEXT,
    )

    response = APIClient().get(
        reverse("api:delivery:content-detail", args=[source.content_id])
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["related"] is None
    assert [item["id"] for item in data["items"]] == [str(kept.content_id)]
    assert "Archived shark" not in response.content.decode()


# --- referenced-by -----------------------------------------------------------


def _use_reference_types(
    monkeypatch: pytest.MonkeyPatch,
) -> RegistryContentTypeService:
    content_types = _reference_service()
    monkeypatch.setattr(services, "get_content_type_service", lambda: content_types)
    monkeypatch.setattr(
        delivery_views, "get_content_type_service", lambda: content_types
    )
    return content_types


def _drain_events() -> None:
    queue = DjangoOutboxQueue(worker_id="test-worker")
    while run_worker_batch(
        queue,
        dispatch=dispatch_content_event,
        visibility_timeout=timedelta(seconds=30),
    ):
        pass


def _referenced_by(content_id: UUID, **params: str | int) -> dict[str, object]:
    response = APIClient().get(
        reverse("api:delivery:content-referenced-by", args=[content_id]), params
    )
    assert response.status_code == 200, response.content
    body: dict[str, object] = response.json()
    return body


def _item_ids(body: dict[str, object]) -> list[str]:
    items = body["items"]
    assert isinstance(items, list)
    return [item["content_id"] for item in items]


def test_referenced_by_lists_only_published_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_types = _use_reference_types(monkeypatch)
    target = _write(content_types, _SPECIES, {"title": "Great white"})
    _publish_result(content_types, target)
    unpublished_target = _write(content_types, _SPECIES, {"title": "Secret"})
    ref = {"related": {"$ref": str(target.content_id)}}
    published_source = _write(content_types, _PROFILE, {**ref, "name": "Pub"})
    _publish_result(content_types, published_source)
    _write(content_types, _PROFILE, {**ref, "name": "Draft only"})
    archived_source = _write(content_types, _PROFILE, {**ref, "name": "Archived"})
    archived_version = _publish_result(content_types, archived_source)
    unrelated = _write(content_types, _PROFILE, {"name": "No refs"})
    _publish_result(content_types, unrelated)
    _drain_events()
    archive_content(
        ArchiveContent(
            content_id=archived_source.content_id,
            expected_version=archived_version,
            actor_id=_ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        policy=_AllowAll(),
        actor=_ACTOR_CONTEXT,
    )

    # Archived sources are excluded at query time even before the event runs.
    body = _referenced_by(target.content_id)

    assert _item_ids(body) == [str(published_source.content_id)]
    assert body["total_count"] == 1
    assert body["limit"] == 20
    assert body["offset"] == 0
    listing = APIClient().get(reverse("api:delivery:content-list")).json()
    by_id = {item["content_id"]: item for item in listing["results"]}
    items = body["items"]
    assert isinstance(items, list)
    assert items[0] == by_id[str(published_source.content_id)]
    assert items[0]["data"]["related"]["title"] == "Great white"
    assert "Draft only" not in str(body)

    anonymous = APIClient()
    unpublished = anonymous.get(
        reverse(
            "api:delivery:content-referenced-by",
            args=[unpublished_target.content_id],
        )
    )
    missing = anonymous.get(
        reverse("api:delivery:content-referenced-by", args=[uuid4()])
    )
    assert unpublished.status_code == 404
    assert missing.status_code == 404


def test_referenced_by_filters_by_type(monkeypatch: pytest.MonkeyPatch) -> None:
    content_types = _use_reference_types(monkeypatch)
    target = _write(content_types, _SPECIES, {"title": "Hammerhead"})
    _publish_result(content_types, target)
    ref: dict[str, object] = {"related": {"$ref": str(target.content_id)}}
    profile = _write(content_types, _PROFILE, ref)
    _publish_result(content_types, profile)
    guide = _write(content_types, _GUIDE, ref)
    _publish_result(content_types, guide)
    many = _write(
        content_types,
        _PROFILE,
        {
            "related": {"$ref": str(target.content_id)},
            "items": [{"$ref": str(target.content_id)}],
        },
    )
    _publish_result(content_types, many)
    _drain_events()

    profiles = _referenced_by(target.content_id, type=str(_PROFILE))
    guides = _referenced_by(target.content_id, type=str(_GUIDE))
    unknown = _referenced_by(target.content_id, type="tests.unknown")
    first_page = _referenced_by(target.content_id, limit=2)
    second_page = _referenced_by(target.content_id, limit=2, offset=2)

    assert sorted(_item_ids(profiles)) == sorted(
        [str(profile.content_id), str(many.content_id)]
    )
    assert profiles["total_count"] == 2
    assert _item_ids(guides) == [str(guide.content_id)]
    assert unknown == {"items": [], "total_count": 0, "limit": 20, "offset": 0}
    assert first_page["total_count"] == 3
    assert len(_item_ids(first_page)) == 2
    assert len(_item_ids(second_page)) == 1
    assert set(_item_ids(first_page) + _item_ids(second_page)) == {
        str(profile.content_id),
        str(guide.content_id),
        str(many.content_id),
    }


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "-1"},
        {"limit": "abc"},
        {"limit": "101"},
        {"offset": "-1"},
        {"type": "not a valid key"},
    ],
)
def test_referenced_by_rejects_invalid_query(
    monkeypatch: pytest.MonkeyPatch, params: dict[str, str]
) -> None:
    content_types = _use_reference_types(monkeypatch)
    target = _write(content_types, _SPECIES, {"title": "Tiger"})
    _publish_result(content_types, target)

    response = APIClient().get(
        reverse("api:delivery:content-referenced-by", args=[target.content_id]),
        params,
    )

    assert response.status_code == 400
