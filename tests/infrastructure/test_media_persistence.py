from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from django.core.files.storage import FileSystemStorage
from django.db import IntegrityError

from strata_cms.application.errors import NotAuthorizedError
from strata_cms.application.ports.authorization import ContentAction, ContentActor
from strata_cms.domain.media import MediaBlob
from strata_cms.domain.value_objects import ActorId
from strata_cms.infrastructure.authorization.policy import StaffPermissionContentPolicy
from strata_cms.infrastructure.media.storage import (
    DjangoMediaFileStore,
    StorageKeyConflictError,
)
from strata_cms.infrastructure.persistence.django.media import (
    DjangoMediaBlobRepository,
)
from strata_cms.infrastructure.persistence.django.models import (
    MediaBlobRecord,
    MediaBlobUsageRecord,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _blob(*, created_at: datetime = NOW, blob_id: UUID | None = None) -> MediaBlob:
    blob_id = blob_id or uuid4()
    return MediaBlob(
        id=blob_id,
        storage_key=f"media/{blob_id}.png",
        sha256="a" * 64,
        content_type="image/png",
        size_bytes=10,
        width=3,
        height=2,
        created_at=created_at,
        created_by=ActorId("7"),
    )


@pytest.mark.django_db
def test_repository_round_trips_blobs() -> None:
    repository = DjangoMediaBlobRepository()
    blob = _blob()
    repository.add(blob)

    assert repository.get_many(frozenset({blob.id, uuid4()})) == {blob.id: blob}
    assert repository.get_many(frozenset()) == {}


@pytest.mark.django_db
def test_repository_rejects_duplicate_storage_key() -> None:
    repository = DjangoMediaBlobRepository()
    blob = _blob()
    repository.add(blob)

    with pytest.raises(IntegrityError):
        repository.add(replace(blob, id=uuid4()))


@pytest.mark.django_db
def test_list_orphans_excludes_used_and_recent_blobs_oldest_first() -> None:
    repository = DjangoMediaBlobRepository()
    oldest = _blob(created_at=NOW - timedelta(days=3))
    older = _blob(created_at=NOW - timedelta(days=2))
    used = _blob(created_at=NOW - timedelta(days=4))
    recent = _blob(created_at=NOW)
    for blob in (recent, older, used, oldest):
        repository.add(blob)
    MediaBlobUsageRecord.objects.create(
        blob_id=used.id, content_id=uuid4(), revision_id=uuid4()
    )

    cutoff = NOW - timedelta(hours=24)
    assert repository.list_orphans(created_before=cutoff, limit=10) == (
        oldest,
        older,
    )
    assert repository.list_orphans(created_before=cutoff, limit=1) == (oldest,)


@pytest.mark.django_db
def test_delete_removes_only_that_blob_and_is_idempotent() -> None:
    repository = DjangoMediaBlobRepository()
    kept, removed = _blob(), _blob()
    repository.add(kept)
    repository.add(removed)

    repository.delete(removed.id)
    repository.delete(removed.id)

    assert set(MediaBlobRecord.objects.values_list("id", flat=True)) == {kept.id}


@pytest.mark.django_db
def test_record_without_dimensions_is_reported() -> None:
    blob = _blob()
    MediaBlobRecord.objects.create(
        id=blob.id,
        storage_key=blob.storage_key,
        sha256=blob.sha256,
        content_type=blob.content_type,
        size_bytes=1,
        created_at=NOW,
        created_by="7",
    )

    with pytest.raises(ValueError, match="dimensions"):
        DjangoMediaBlobRepository().get_many(frozenset({blob.id}))


def test_file_store_saves_urls_and_deletes(tmp_path: Path) -> None:
    store = DjangoMediaFileStore(
        FileSystemStorage(location=tmp_path, base_url="/media/")
    )
    key = "media/ab/cd/blob.png"

    store.save(key, b"bytes")

    assert (tmp_path / key).read_bytes() == b"bytes"
    assert store.url(key) == "/media/media/ab/cd/blob.png"
    store.delete(key)
    store.delete(key)
    assert not (tmp_path / key).exists()


def test_file_store_refuses_renamed_save(tmp_path: Path) -> None:
    store = DjangoMediaFileStore(FileSystemStorage(location=tmp_path))
    key = "media/blob.png"
    store.save(key, b"first")

    with pytest.raises(StorageKeyConflictError):
        store.save(key, b"second")

    assert (tmp_path / key).read_bytes() == b"first"
    assert [path.name for path in (tmp_path / "media").iterdir()] == ["blob.png"]


def test_upload_media_requires_upload_permission() -> None:
    policy = StaffPermissionContentPolicy()
    uploader = ContentActor(
        id=ActorId("1"),
        is_staff=True,
        permissions=frozenset({"strata_persistence.upload_mediablobrecord"}),
    )
    editor = replace(
        uploader, permissions=frozenset({"strata_persistence.change_contentrecord"})
    )

    policy.authorize(uploader, ContentAction.UPLOAD_MEDIA)
    with pytest.raises(NotAuthorizedError):
        policy.authorize(editor, ContentAction.UPLOAD_MEDIA)


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        (lambda blob: replace(blob, storage_key=""), "storage key"),
        (lambda blob: replace(blob, sha256="XYZ"), "sha256"),
        (lambda blob: replace(blob, size_bytes=-1), "size"),
        (lambda blob: replace(blob, width=0), "dimensions"),
        (
            lambda blob: replace(blob, created_at=datetime(2026, 1, 1)),  # noqa: DTZ001
            "timezone",
        ),
    ],
)
def test_media_blob_invariants(
    invalid: Callable[[MediaBlob], MediaBlob], message: str
) -> None:
    blob = _blob()
    with pytest.raises(ValueError, match=message):
        invalid(blob)
