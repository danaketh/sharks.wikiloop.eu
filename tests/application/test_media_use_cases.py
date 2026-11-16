import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

import pytest

from strata_cms.application.errors import NotAuthorizedError, UnsupportedMediaError
from strata_cms.application.media.use_cases import UploadMediaBlob, upload_media_blob
from strata_cms.application.ports.authorization import ContentAction, ContentActor
from strata_cms.application.ports.media import ProcessedImage
from strata_cms.domain.media import MediaBlob
from strata_cms.domain.value_objects import ActorId

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
ACTOR = ContentActor(id=ActorId("7"), is_staff=True, permissions=frozenset())
BLOB_ID = UUID("abcdef01-2345-4678-9abc-def012345678")
MAX_BYTES = 10


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FixedIds:
    def new_uuid(self) -> UUID:
        return BLOB_ID


class RecordingPolicy:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.actions: list[ContentAction] = []

    def authorize(self, actor: ContentActor, action: ContentAction) -> None:
        del actor
        self.actions.append(action)
        if not self.allow:
            raise NotAuthorizedError(action.value)


class FakeProcessor:
    def __init__(self, *, error: UnsupportedMediaError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[bytes, bool]] = []

    def process(self, data: bytes, *, preserve_original: bool) -> ProcessedImage:
        self.calls.append((data, preserve_original))
        if self.error is not None:
            raise self.error
        return ProcessedImage(
            data=b"clean:" + data,
            content_type="image/png",
            extension="png",
            width=3,
            height=2,
        )


class FakeFiles:
    def __init__(self) -> None:
        self.saved: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def save(self, key: str, data: bytes) -> None:
        self.saved[key] = data

    def url(self, key: str) -> str:
        return f"/media/{key}"

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.saved.pop(key, None)


class FakeBlobs:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.added: list[MediaBlob] = []

    def add(self, blob: MediaBlob) -> None:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.added.append(blob)

    def get_many(self, ids: frozenset[UUID]) -> Mapping[UUID, MediaBlob]:
        return {blob.id: blob for blob in self.added if blob.id in ids}

    def list_orphans(
        self, *, created_before: datetime, limit: int
    ) -> tuple[MediaBlob, ...]:
        del created_before, limit
        return ()

    def delete(self, blob_id: UUID) -> None:
        del blob_id


def _upload(
    data: bytes = b"bytes",
    *,
    preserve_original: bool = False,
    processor: FakeProcessor | None = None,
    files: FakeFiles | None = None,
    blobs: FakeBlobs | None = None,
    policy: RecordingPolicy | None = None,
) -> MediaBlob:
    return upload_media_blob(
        UploadMediaBlob(
            actor_id=ACTOR.id, data=data, preserve_original=preserve_original
        ),
        processor=processor or FakeProcessor(),
        files=files or FakeFiles(),
        blobs=blobs or FakeBlobs(),
        ids=FixedIds(),
        clock=FixedClock(),
        policy=policy or RecordingPolicy(),
        actor=ACTOR,
        max_bytes=MAX_BYTES,
    )


def test_upload_saves_processed_bytes_then_records_blob() -> None:
    files = FakeFiles()
    blobs = FakeBlobs()
    processor = FakeProcessor()

    blob = _upload(b"raw", files=files, blobs=blobs, processor=processor)

    key = f"media/ab/cd/{BLOB_ID}.png"
    assert files.saved == {key: b"clean:raw"}
    assert blobs.added == [blob]
    assert processor.calls == [(b"raw", False)]
    assert blob == MediaBlob(
        id=BLOB_ID,
        storage_key=key,
        sha256=hashlib.sha256(b"clean:raw").hexdigest(),
        content_type="image/png",
        size_bytes=len(b"clean:raw"),
        width=3,
        height=2,
        created_at=NOW,
        created_by=ACTOR.id,
    )


def test_preserve_original_is_forwarded_and_uses_upload_permission() -> None:
    policy = RecordingPolicy()
    processor = FakeProcessor()

    _upload(preserve_original=True, processor=processor, policy=policy)

    assert processor.calls == [(b"bytes", True)]
    assert policy.actions == [ContentAction.UPLOAD_MEDIA]


def test_upload_of_exactly_max_bytes_is_accepted() -> None:
    blob = _upload(b"x" * MAX_BYTES)

    assert blob.id == BLOB_ID


def test_upload_over_max_bytes_is_rejected_before_processing() -> None:
    processor = FakeProcessor()
    files = FakeFiles()

    with pytest.raises(UnsupportedMediaError) as error:
        _upload(b"x" * (MAX_BYTES + 1), processor=processor, files=files)

    assert error.value.reason == "media_too_large"
    assert processor.calls == []
    assert files.saved == {}


def test_unauthorized_actor_is_rejected_before_processing() -> None:
    processor = FakeProcessor()

    with pytest.raises(NotAuthorizedError):
        _upload(processor=processor, policy=RecordingPolicy(allow=False))

    assert processor.calls == []


def test_processor_rejection_saves_nothing() -> None:
    files = FakeFiles()
    blobs = FakeBlobs()

    with pytest.raises(UnsupportedMediaError):
        _upload(
            processor=FakeProcessor(error=UnsupportedMediaError("media_unreadable")),
            files=files,
            blobs=blobs,
        )

    assert files.saved == {}
    assert blobs.added == []


def test_repository_failure_deletes_saved_file_and_reraises() -> None:
    files = FakeFiles()

    with pytest.raises(RuntimeError, match="database unavailable"):
        _upload(files=files, blobs=FakeBlobs(fail=True))

    key = f"media/ab/cd/{BLOB_ID}.png"
    assert files.deleted == [key]
    assert files.saved == {}


def test_unknown_rejection_reason_is_refused() -> None:
    with pytest.raises(ValueError, match="reason"):
        UnsupportedMediaError("anything")
