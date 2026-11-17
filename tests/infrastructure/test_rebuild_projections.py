from datetime import UTC, datetime, timedelta
from io import StringIO
from uuid import UUID, uuid4

import pytest
from django.core.management import CommandError, call_command

from strata_cms.application.content.commands import (
    ArchiveContent,
    CreateContent,
    CreateRevision,
    PublishContent,
)
from strata_cms.application.content.projection_rebuild import (
    RebuildScope,
    rebuild_projections,
)
from strata_cms.application.content.results import ContentWriteResult
from strata_cms.application.content.use_cases import (
    archive_content,
    create_content,
    create_revision,
    publish_content,
)
from strata_cms.application.ports.authorization import ContentAction, ContentActor
from strata_cms.config import services
from strata_cms.config.services import dispatch_content_event
from strata_cms.domain.revision import RevisionData
from strata_cms.domain.value_objects import ActorId, ContentId, ContentTypeKey
from strata_cms.infrastructure.clock.system import DjangoClock
from strata_cms.infrastructure.content_types import RegistryContentTypeService
from strata_cms.infrastructure.ids.uuid import Uuid4Generator
from strata_cms.infrastructure.persistence.django.management.commands import (
    strata_rebuild_projections,
)
from strata_cms.infrastructure.persistence.django.mappers import revision_from_record
from strata_cms.infrastructure.persistence.django.media import (
    DjangoMediaBlobRepository,
)
from strata_cms.infrastructure.persistence.django.models import (
    ContentRecord,
    ContentReferenceRecord,
    ContentTitleRecord,
    MediaBlobRecord,
    MediaBlobUsageRecord,
    RevisionRecord,
)
from strata_cms.infrastructure.persistence.django.outbox import DjangoOutboxQueue
from strata_cms.infrastructure.persistence.django.projections import (
    DjangoLinkTargetQueries,
)
from strata_cms.infrastructure.persistence.django.published_references import (
    DjangoPublishedReferenceIndex,
)
from strata_cms.infrastructure.persistence.django.rebuild import (
    DjangoProjectionRebuildUnitOfWork,
)
from strata_cms.infrastructure.persistence.django.unit_of_work import (
    DjangoContentUnitOfWork,
)
from strata_cms.infrastructure.tasks.worker import run_worker_batch
from strata_cms.plugin_api import (
    BlobFieldDefinition,
    ContentTypeDefinition,
    LinkPath,
    PluginDefinition,
    PluginKey,
    ReferenceFieldDefinition,
    SchemaMigration,
    SchemaMigrationSet,
    StrataRegistryBuilder,
    ValidationIssue,
)

pytestmark = pytest.mark.django_db

SPECIES = ContentTypeKey("tests.species")
PROFILE = ContentTypeKey("tests.profile")
ACTOR = ActorId("user:1")
ACTOR_CONTEXT = ContentActor(id=ACTOR, is_staff=True, permissions=frozenset())
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


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


def _service() -> RegistryContentTypeService:
    builder = StrataRegistryBuilder()
    plugin = PluginDefinition(
        key=PluginKey("tests.rebuild"), version="1.0.0", requires_strata=">=0.1,<0.2"
    )
    builder.register_plugin(plugin)
    registrar = builder.registrar_for(plugin.key)
    registrar.register_content_type(
        ContentTypeDefinition(
            key=SPECIES,
            schema_version=1,
            schema=_DictSchema(),
            summary_title=lambda value: str(value.get("title", "")),
        )
    )
    registrar.register_content_type(
        ContentTypeDefinition(
            key=PROFILE,
            schema_version=1,
            schema=_DictSchema(),
            reference_fields=(
                ReferenceFieldDefinition(
                    LinkPath.parse("related"), frozenset({SPECIES})
                ),
                ReferenceFieldDefinition(
                    LinkPath.parse("items"), frozenset({SPECIES}), many=True
                ),
            ),
            blob_fields=(BlobFieldDefinition(LinkPath.parse("image")),),
            summary_title=lambda value: str(value.get("name", "")),
        )
    )
    return RegistryContentTypeService(builder.build(strata_version="0.1.0"))


@pytest.fixture
def content_types(monkeypatch: pytest.MonkeyPatch) -> RegistryContentTypeService:
    service = _service()
    monkeypatch.setattr(services, "get_content_type_service", lambda: service)
    monkeypatch.setattr(
        strata_rebuild_projections, "_content_type_service", lambda: service
    )
    return service


def _create(
    content_types: RegistryContentTypeService,
    type_key: ContentTypeKey,
    data: dict[str, object],
) -> ContentWriteResult:
    return create_content(
        CreateContent(type_key=type_key, schema_version=1, data=data, actor_id=ACTOR),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=content_types,
        link_targets=DjangoLinkTargetQueries(),
        policy=_AllowAll(),
        actor=ACTOR_CONTEXT,
    )


def _revise(
    content_types: RegistryContentTypeService,
    written: ContentWriteResult,
    data: dict[str, object],
    *,
    expected_version: int,
) -> ContentWriteResult:
    return create_revision(
        CreateRevision(
            content_id=written.content_id,
            expected_version=expected_version,
            schema_version=1,
            data=data,
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=content_types,
        link_targets=DjangoLinkTargetQueries(),
        policy=_AllowAll(),
        actor=ACTOR_CONTEXT,
    )


def _publish(
    content_types: RegistryContentTypeService, written: ContentWriteResult
) -> int:
    return publish_content(
        PublishContent(
            content_id=written.content_id,
            revision_id=written.revision_id,
            expected_version=written.content_version,
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=content_types,
        policy=_AllowAll(),
        actor=ACTOR_CONTEXT,
    ).content_version


def _archive(content_id: ContentId, expected_version: int) -> None:
    archive_content(
        ArchiveContent(
            content_id=content_id, expected_version=expected_version, actor_id=ACTOR
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        policy=_AllowAll(),
        actor=ACTOR_CONTEXT,
    )


def _blob() -> UUID:
    blob_id = uuid4()
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


def _drain_events() -> None:
    queue = DjangoOutboxQueue(worker_id="test-worker")
    while run_worker_batch(
        queue,
        dispatch=dispatch_content_event,
        visibility_timeout=timedelta(seconds=30),
    ):
        pass


def _ref(content_id: UUID) -> dict[str, str]:
    return {"$ref": str(content_id)}


def _snapshot() -> dict[str, set[tuple[object, ...]]]:
    return {
        "titles": set(
            ContentTitleRecord.objects.values_list(
                "content_id", "type_key", "title", "archived"
            )
        ),
        "draft_references": set(
            ContentReferenceRecord.objects.filter(scope="draft").values_list(
                "source_id",
                "source_revision_id",
                "location",
                "target_id",
                "target_type",
            )
        ),
        "published_references": set(
            ContentReferenceRecord.objects.filter(scope="published").values_list(
                "source_id",
                "source_revision_id",
                "location",
                "target_id",
                "target_type",
            )
        ),
        "blob_usage": set(
            MediaBlobUsageRecord.objects.values_list(
                "blob_id", "content_id", "revision_id"
            )
        ),
        "blobs": set(MediaBlobRecord.objects.values_list("id", "sha256")),
    }


def _build_incrementally(
    content_types: RegistryContentTypeService,
) -> dict[str, ContentWriteResult]:
    first_blob, second_blob = _blob(), _blob()
    mako = _create(content_types, SPECIES, {"title": "Mako"})
    _publish(content_types, mako)
    tiger = _create(content_types, SPECIES, {"title": "Tiger"})
    tiger_version = _publish(content_types, tiger)
    tiger_draft = _revise(
        content_types, tiger, {"title": "Tiger draft"}, expected_version=tiger_version
    )

    profile = _create(
        content_types,
        PROFILE,
        {
            "name": "Reef",
            "related": _ref(mako.content_id),
            "image": {"$blob": str(first_blob)},
        },
    )
    profile_version = _publish(content_types, profile)
    _revise(
        content_types,
        profile,
        {
            "name": "Reef v2",
            "related": _ref(tiger.content_id),
            "items": [_ref(mako.content_id), _ref(tiger.content_id)],
            "image": {"$blob": str(second_blob)},
        },
        expected_version=profile_version,
    )

    archived = _create(
        content_types, PROFILE, {"name": "Gone", "related": _ref(mako.content_id)}
    )
    _drain_events()
    _archive(archived.content_id, _publish(content_types, archived))
    _drain_events()
    return {"mako": mako, "tiger": tiger, "tiger_draft": tiger_draft}


def test_rebuild_reproduces_incrementally_built_state(
    content_types: RegistryContentTypeService,
) -> None:
    written = _build_incrementally(content_types)
    expected = _snapshot()
    assert expected["published_references"], "fixture must index published refs"
    assert any(row[3] for row in expected["titles"]), "fixture must archive one"
    assert len(expected["blob_usage"]) == 2

    # Simulate pre-0007 content plus stale/foreign rows the rebuild must drop.
    ContentTitleRecord.objects.filter(pk=written["mako"].content_id).delete()
    ContentReferenceRecord.objects.filter(scope="draft").delete()
    MediaBlobUsageRecord.objects.all().delete()
    ContentReferenceRecord.objects.create(
        scope="published",
        source_id=uuid4(),
        source_revision_id=uuid4(),
        location="stale",
        target_id=written["tiger"].content_id,
        target_type=str(SPECIES),
    )
    assert _snapshot() != expected

    report = rebuild_projections(
        RebuildScope.ALL,
        new_uow=DjangoProjectionRebuildUnitOfWork,
        content_types=content_types,
    )

    assert _snapshot() == expected
    assert report.skipped_links == ()
    assert report.skipped_revisions == ()
    assert report.draft_revisions == ContentRecord.objects.count() + 2
    assert report.published_revisions == 3


def test_rebuild_skips_and_reports_missing_targets(
    content_types: RegistryContentTypeService,
) -> None:
    doomed = _create(content_types, SPECIES, {"title": "Doomed"})
    kept = _create(content_types, SPECIES, {"title": "Kept"})
    _publish(content_types, kept)
    blob = _blob()
    source = _create(
        content_types,
        PROFILE,
        {
            "name": "Src",
            "items": [_ref(doomed.content_id), _ref(kept.content_id)],
            "image": {"$blob": str(blob)},
        },
    )
    _publish(content_types, source)
    # Out-of-band deletion of a referenced target and blob.
    ContentRecord.objects.filter(pk=doomed.content_id).update(
        latest_revision=None, latest_revision_number=0
    )
    ContentRecord.objects.filter(pk=doomed.content_id).delete()
    MediaBlobRecord.objects.filter(pk=blob).delete()

    report = rebuild_projections(
        RebuildScope.ALL,
        new_uow=DjangoProjectionRebuildUnitOfWork,
        content_types=content_types,
    )

    for scope in ("draft", "published"):
        assert list(
            ContentReferenceRecord.objects.filter(
                scope=scope, source_id=source.content_id
            ).values_list("location", "target_id")
        ) == [("items/1", kept.content_id)]
    assert not MediaBlobUsageRecord.objects.filter(blob_id=blob).exists()
    skipped = {
        (link.scope, link.kind, link.target, link.location)
        for link in report.skipped_links
    }
    assert skipped == {
        ("draft", "reference", doomed.content_id, "items/0"),
        ("published", "reference", doomed.content_id, "items/0"),
        ("draft", "blob", blob, "image"),
    }


def test_command_rebuilds_requested_scope_only(
    content_types: RegistryContentTypeService,
) -> None:
    target = _create(content_types, SPECIES, {"title": "Blue"})
    _publish(content_types, target)
    source = _create(
        content_types, PROFILE, {"name": "Src", "related": _ref(target.content_id)}
    )
    _publish(content_types, source)
    ContentReferenceRecord.objects.all().delete()

    out = StringIO()
    call_command("strata_rebuild_projections", "--scope", "draft", stdout=out)

    assert set(ContentReferenceRecord.objects.values_list("scope", flat=True)) == {
        "draft"
    }
    assert "draft" in out.getvalue()

    call_command("strata_rebuild_projections", stdout=out)

    assert set(ContentReferenceRecord.objects.values_list("scope", flat=True)) == {
        "draft",
        "published",
    }
    with pytest.raises(CommandError):
        call_command("strata_rebuild_projections", "--scope", "everything")


def test_published_index_ignores_stale_revision(
    content_types: RegistryContentTypeService,
) -> None:
    target = _create(content_types, SPECIES, {"title": "Lemon"})
    _publish(content_types, target)
    source = _create(
        content_types, PROFILE, {"name": "Src", "related": _ref(target.content_id)}
    )
    version = _publish(content_types, source)
    newer = _revise(content_types, source, {"name": "Src v2"}, expected_version=version)
    _publish(content_types, newer)
    index = DjangoPublishedReferenceIndex()
    links = content_types.revision_links(
        revision_from_record(RevisionRecord.objects.get(pk=source.revision_id))
    )

    # A slow handler still holding the old published revision must not win.
    index.replace(source.content_id, source.revision_id, links.references)

    assert not ContentReferenceRecord.objects.filter(scope="published").exists()


def test_rebuild_reports_revisions_of_uninstalled_types(
    content_types: RegistryContentTypeService,
) -> None:
    kept = _create(content_types, SPECIES, {"title": "Whale shark"})
    _publish(content_types, kept)
    orphan = _create(content_types, PROFILE, {"name": "Orphan"})
    _publish(content_types, orphan)
    builder = StrataRegistryBuilder()
    plugin = PluginDefinition(
        key=PluginKey("tests.rebuild"), version="1.0.0", requires_strata=">=0.1,<0.2"
    )
    builder.register_plugin(plugin)
    builder.registrar_for(plugin.key).register_content_type(
        ContentTypeDefinition(
            key=SPECIES,
            schema_version=1,
            schema=_DictSchema(),
            summary_title=lambda value: str(value.get("title", "")),
        )
    )
    species_only = RegistryContentTypeService(builder.build(strata_version="0.1.0"))

    report = rebuild_projections(
        RebuildScope.ALL,
        new_uow=DjangoProjectionRebuildUnitOfWork,
        content_types=species_only,
    )

    assert {(item.scope, item.content_id) for item in report.skipped_revisions} == {
        ("draft", orphan.content_id),
        ("published", orphan.content_id),
    }
    assert set(ContentTitleRecord.objects.values_list("content_id", flat=True)) == {
        kept.content_id
    }
    assert report.draft_revisions == 1
    assert report.published_revisions == 1


def test_rebuild_keeps_blob_usage_of_undecodable_revisions(
    content_types: RegistryContentTypeService,
) -> None:
    """Orphan cleanup relies on usage rows; losing them would delete files."""
    nested_blob, image_blob, missing_blob = _blob(), _blob(), uuid4()
    orphan = _create(
        content_types,
        PROFILE,
        {"name": "Orphan", "image": {"$blob": str(image_blob)}},
    )
    broken = RevisionRecord.objects.get(pk=orphan.revision_id)
    broken.data = {
        "name": "Orphan",
        "image": {"$blob": str(image_blob)},
        "body": [{"data": {"photo": {"$blob": str(nested_blob)}}}],
        "gone": {"$blob": str(missing_blob)},
        "noise": [{"$blob": "not-a-uuid"}, {"$blob": 3}],
    }
    broken.save(update_fields=["data"])
    uninstalled = _create(
        content_types, PROFILE, {"name": "Gone", "image": {"$blob": str(image_blob)}}
    )
    species_only = _species_only_service()

    report = rebuild_projections(
        RebuildScope.DRAFT,
        new_uow=DjangoProjectionRebuildUnitOfWork,
        content_types=species_only,
    )

    assert len(report.skipped_revisions) == 2
    assert set(MediaBlobUsageRecord.objects.values_list("blob_id", "revision_id")) == {
        (image_blob, orphan.revision_id),
        (nested_blob, orphan.revision_id),
        (image_blob, uninstalled.revision_id),
    }
    assert not ContentTitleRecord.objects.exists()


def _species_only_service() -> RegistryContentTypeService:
    builder = StrataRegistryBuilder()
    plugin = PluginDefinition(
        key=PluginKey("tests.rebuild"), version="1.0.0", requires_strata=">=0.1,<0.2"
    )
    builder.register_plugin(plugin)
    builder.registrar_for(plugin.key).register_content_type(
        ContentTypeDefinition(
            key=SPECIES,
            schema_version=1,
            schema=_DictSchema(),
            summary_title=lambda value: str(value.get("title", "")),
        )
    )
    return RegistryContentTypeService(builder.build(strata_version="0.1.0"))


def test_published_index_remove_keeps_rows_of_currently_published_source(
    content_types: RegistryContentTypeService,
) -> None:
    """Interleaving: a stale "not published" read must not drop current rows."""
    target = _create(content_types, SPECIES, {"title": "Nurse"})
    _publish(content_types, target)
    source = _create(
        content_types, PROFILE, {"name": "Src", "related": _ref(target.content_id)}
    )
    version = _publish(content_types, source)
    index = DjangoPublishedReferenceIndex()
    links = content_types.revision_links(
        revision_from_record(RevisionRecord.objects.get(pk=source.revision_id))
    )
    index.replace(source.content_id, source.revision_id, links.references)

    # Worker A read "not published" earlier; B already re-indexed the source.
    index.remove(source.content_id)

    published_rows = ContentReferenceRecord.objects.filter(
        scope="published", source_id=source.content_id
    )
    assert published_rows.count() == 1

    _archive(source.content_id, version)
    index.remove(source.content_id)

    assert not published_rows.exists()


def test_published_index_remove_drops_rows_of_unpublished_source(
    content_types: RegistryContentTypeService,
) -> None:
    target = _create(content_types, SPECIES, {"title": "Basking"})
    _publish(content_types, target)
    draft_only = _create(
        content_types, PROFILE, {"name": "Draft", "related": _ref(target.content_id)}
    )
    ContentReferenceRecord.objects.create(
        scope="published",
        source_id=draft_only.content_id,
        source_revision_id=draft_only.revision_id,
        location="related",
        target_id=target.content_id,
        target_type=str(SPECIES),
    )
    missing = ContentId(uuid4())
    ContentReferenceRecord.objects.create(
        scope="published",
        source_id=missing,
        source_revision_id=uuid4(),
        location="related",
        target_id=target.content_id,
        target_type=str(SPECIES),
    )

    index = DjangoPublishedReferenceIndex()
    index.remove(draft_only.content_id)
    index.remove(missing)

    assert not ContentReferenceRecord.objects.filter(scope="published").exists()


def _profile_v2_without_image() -> RegistryContentTypeService:
    """PROFILE v2 whose migration drops the v1 ``image`` blob field."""

    def drop_image(data: RevisionData) -> RevisionData:
        migrated = dict(data.as_dict())
        migrated.pop("image", None)
        return RevisionData.from_mapping(migrated)

    builder = StrataRegistryBuilder()
    plugin = PluginDefinition(
        key=PluginKey("tests.rebuild"), version="2.0.0", requires_strata=">=0.1,<0.2"
    )
    builder.register_plugin(plugin)
    builder.registrar_for(plugin.key).register_content_type(
        ContentTypeDefinition(
            key=PROFILE,
            schema_version=2,
            schema=_DictSchema(),
            migrations=SchemaMigrationSet(
                (SchemaMigration(from_version=1, to_version=2, migrate=drop_image),)
            ),
            summary_title=lambda value: str(value.get("name", "")),
        )
    )
    return RegistryContentTypeService(builder.build(strata_version="0.1.0"))


def test_rebuild_keeps_blob_usage_dropped_by_schema_migration(
    content_types: RegistryContentTypeService,
) -> None:
    """Stored history still links the blob even if the current schema drops it."""
    blob = _blob()
    profile = _create(
        content_types, PROFILE, {"name": "Old", "image": {"$blob": str(blob)}}
    )
    MediaBlobUsageRecord.objects.all().delete()

    report = rebuild_projections(
        RebuildScope.DRAFT,
        new_uow=DjangoProjectionRebuildUnitOfWork,
        content_types=_profile_v2_without_image(),
    )

    assert report.skipped_revisions == ()
    assert set(MediaBlobUsageRecord.objects.values_list("blob_id", "revision_id")) == {
        (blob, profile.revision_id)
    }
    assert DjangoMediaBlobRepository().delete_if_unused(blob) is False
    assert MediaBlobRecord.objects.filter(pk=blob).exists()


def test_referencing_ignores_rows_of_superseded_published_revision(
    content_types: RegistryContentTypeService,
) -> None:
    target = _create(content_types, SPECIES, {"title": "Thresher"})
    _publish(content_types, target)
    source = _create(
        content_types, PROFILE, {"name": "Src", "related": _ref(target.content_id)}
    )
    version = _publish(content_types, source)
    index = DjangoPublishedReferenceIndex()
    links = content_types.revision_links(
        revision_from_record(RevisionRecord.objects.get(pk=source.revision_id))
    )
    index.replace(source.content_id, source.revision_id, links.references)
    assert index.referencing(
        target.content_id, source_type=None, limit=20, offset=0
    ) == ((source.content_id,), 1)

    # Republished without the link; the index event has not run yet.
    newer = _revise(content_types, source, {"name": "Src v2"}, expected_version=version)
    _publish(content_types, newer)

    assert index.referencing(
        target.content_id, source_type=None, limit=20, offset=0
    ) == ((), 0)


def test_command_help_warns_about_concurrent_writes() -> None:
    help_text = " ".join(strata_rebuild_projections.Command.help.split()).lower()

    assert "does not block concurrent writes" in help_text
    assert "maintenance window" in help_text
