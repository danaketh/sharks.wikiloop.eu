from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from django.conf import settings as django_settings
from django.core.files.storage import FileSystemStorage
from django.core.management import CommandError, call_command
from pytest_django.fixtures import Settings

from strata_cms.application.content.commands import CreateContent, CreateRevision
from strata_cms.application.content.use_cases import create_content, create_revision
from strata_cms.application.media.cleanup import cleanup_orphan_blobs
from strata_cms.application.ports.authorization import ContentAction, ContentActor
from strata_cms.config import services
from strata_cms.contrib.media.schema import MEDIA_CONTENT_TYPE
from strata_cms.domain.media import MediaBlob
from strata_cms.domain.value_objects import ActorId
from strata_cms.infrastructure.clock.system import DjangoClock
from strata_cms.infrastructure.ids.uuid import Uuid4Generator
from strata_cms.infrastructure.media.storage import DjangoMediaFileStore
from strata_cms.infrastructure.persistence.django.media import (
    DjangoMediaBlobRepository,
)
from strata_cms.infrastructure.persistence.django.models import (
    MediaBlobRecord,
    MediaBlobUsageRecord,
)
from strata_cms.infrastructure.persistence.django.projections import (
    DjangoLinkTargetQueries,
)
from strata_cms.infrastructure.persistence.django.unit_of_work import (
    DjangoContentUnitOfWork,
)

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
GRACE = timedelta(hours=24)
ACTOR = ActorId("user:1")
ACTOR_CONTEXT = ContentActor(id=ACTOR, is_staff=True, permissions=frozenset())


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _AllowAll:
    def authorize(self, actor: ContentActor, action: ContentAction) -> None:
        del actor, action


@pytest.fixture
def store(tmp_path: Path) -> DjangoMediaFileStore:
    return DjangoMediaFileStore(FileSystemStorage(location=tmp_path))


def _blob(store: DjangoMediaFileStore, *, age: timedelta) -> MediaBlob:
    blob_id = uuid4()
    blob = MediaBlob(
        id=blob_id,
        storage_key=f"media/{blob_id}.png",
        sha256="a" * 64,
        content_type="image/png",
        size_bytes=5,
        width=3,
        height=2,
        created_at=NOW - age,
        created_by=ACTOR,
    )
    store.save(blob.storage_key, b"bytes")
    DjangoMediaBlobRepository().add(blob)
    return blob


def _exists(blob: MediaBlob) -> bool:
    return MediaBlobRecord.objects.filter(pk=blob.id).exists()


def _file_exists(tmp_path: Path, blob: MediaBlob) -> bool:
    return (tmp_path / blob.storage_key).is_file()


def _cleanup(
    store: DjangoMediaFileStore,
    *,
    blobs: DjangoMediaBlobRepository | None = None,
    batch_size: int = 100,
) -> int:
    return cleanup_orphan_blobs(
        blobs=blobs or DjangoMediaBlobRepository(),
        files=store,
        clock=FixedClock(NOW),
        grace=GRACE,
        batch_size=batch_size,
    )


def _media(blob_id: UUID | None) -> dict[str, object]:
    return {
        "file": {"$blob": str(blob_id)},
        "title": "Shark",
        "alt_text": "Shark",
    }


def test_cleanup_deletes_only_unused_blobs_past_grace(
    store: DjangoMediaFileStore, tmp_path: Path
) -> None:
    stale = [_blob(store, age=GRACE + timedelta(minutes=n + 1)) for n in range(3)]
    fresh = _blob(store, age=GRACE - timedelta(minutes=1))
    used = _blob(store, age=GRACE * 3)
    MediaBlobUsageRecord.objects.create(
        blob_id=used.id, content_id=uuid4(), revision_id=uuid4()
    )

    deleted = _cleanup(store, batch_size=2)

    assert deleted == 3
    for blob in stale:
        assert not _exists(blob)
        assert not _file_exists(tmp_path, blob)
    for blob in (fresh, used):
        assert _exists(blob)
        assert _file_exists(tmp_path, blob)


def test_cleanup_keeps_blob_used_only_by_old_revision(
    store: DjangoMediaFileStore, tmp_path: Path
) -> None:
    old = _blob(store, age=GRACE * 2)
    current = _blob(store, age=GRACE * 2)
    created = create_content(
        CreateContent(
            type_key=MEDIA_CONTENT_TYPE,
            schema_version=1,
            data=_media(old.id),
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=services.get_content_type_service(),
        link_targets=DjangoLinkTargetQueries(),
        policy=_AllowAll(),
        actor=ACTOR_CONTEXT,
    )
    create_revision(
        CreateRevision(
            content_id=created.content_id,
            expected_version=created.content_version,
            schema_version=1,
            data=_media(current.id),
            actor_id=ACTOR,
        ),
        uow=DjangoContentUnitOfWork(clock=DjangoClock()),
        clock=DjangoClock(),
        ids=Uuid4Generator(),
        content_types=services.get_content_type_service(),
        link_targets=DjangoLinkTargetQueries(),
        policy=_AllowAll(),
        actor=ACTOR_CONTEXT,
    )

    assert _cleanup(store) == 0
    assert _exists(old)
    assert _file_exists(tmp_path, old)


def test_cleanup_is_idempotent_when_file_already_gone(
    store: DjangoMediaFileStore, tmp_path: Path
) -> None:
    blob = _blob(store, age=GRACE * 2)
    (tmp_path / blob.storage_key).unlink()

    assert _cleanup(store) == 1
    assert not _exists(blob)
    assert _cleanup(store) == 0


class _UsedAfterListing(DjangoMediaBlobRepository):
    """Simulates a revision referencing a candidate between listing and delete."""

    def list_orphans(
        self, *, created_before: datetime, limit: int
    ) -> tuple[MediaBlob, ...]:
        orphans = super().list_orphans(created_before=created_before, limit=limit)
        for blob in orphans:
            MediaBlobUsageRecord.objects.create(
                blob_id=blob.id, content_id=uuid4(), revision_id=uuid4()
            )
        return orphans


def test_cleanup_keeps_blob_that_became_used_before_delete(
    store: DjangoMediaFileStore, tmp_path: Path
) -> None:
    blob = _blob(store, age=GRACE * 2)

    assert _cleanup(store, blobs=_UsedAfterListing()) == 0
    assert _exists(blob)
    assert _file_exists(tmp_path, blob)


@pytest.mark.parametrize(
    ("grace", "batch_size"), [(timedelta(seconds=-1), 10), (GRACE, 0)]
)
def test_cleanup_rejects_invalid_arguments(
    store: DjangoMediaFileStore, grace: timedelta, batch_size: int
) -> None:
    with pytest.raises(ValueError, match="must"):
        cleanup_orphan_blobs(
            blobs=DjangoMediaBlobRepository(),
            files=store,
            clock=FixedClock(NOW),
            grace=grace,
            batch_size=batch_size,
        )


def test_command_uses_grace_setting_and_prints_count(
    settings: Settings, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = str(tmp_path)
    default_store = DjangoMediaFileStore()
    old_blob_age = NOW - datetime.now(UTC) + timedelta(hours=2)
    stale = _blob(default_store, age=old_blob_age)
    settings.STRATA_MEDIA_ORPHAN_GRACE = timedelta(hours=1)
    out = StringIO()

    call_command("strata_cleanup_media", stdout=out)

    assert out.getvalue().strip() == "deleted 1 orphan media blob(s)"
    assert not _exists(stale)
    assert not _file_exists(tmp_path, stale)


def test_default_grace_setting_is_24_hours() -> None:

    assert timedelta(hours=24) == django_settings.STRATA_MEDIA_ORPHAN_GRACE


def test_command_rejects_non_timedelta_grace(settings: Settings) -> None:
    settings.STRATA_MEDIA_ORPHAN_GRACE = 24

    with pytest.raises(CommandError, match="timedelta"):
        call_command("strata_cleanup_media")
