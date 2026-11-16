"""Media blob upload use case shared by API/Admin/CLI callers."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from strata_cms.application.errors import UnsupportedMediaError
from strata_cms.application.ports.authorization import (
    ContentAction,
    ContentActor,
    ContentAuthorizationPolicy,
)
from strata_cms.application.ports.clock import Clock
from strata_cms.application.ports.ids import IdGenerator
from strata_cms.application.ports.media import (
    ImageProcessor,
    MediaBlobRepository,
    MediaFileStore,
)
from strata_cms.domain.media import MediaBlob
from strata_cms.domain.value_objects import ActorId


@dataclass(frozen=True, slots=True)
class UploadMediaBlob:
    """Upload untrusted bytes as a new immutable media blob."""

    actor_id: ActorId
    data: bytes
    preserve_original: bool


def media_storage_key(blob_id: UUID, extension: str) -> str:
    """Return the server-generated storage key for a blob.

    The extension always comes from the sniffed type, never from the client.
    """
    hex_id = blob_id.hex
    return f"media/{hex_id[0:2]}/{hex_id[2:4]}/{blob_id}.{extension}"


def upload_media_blob(  # noqa: PLR0913 - explicit port injection per use case
    command: UploadMediaBlob,
    *,
    processor: ImageProcessor,
    files: MediaFileStore,
    blobs: MediaBlobRepository,
    ids: IdGenerator,
    clock: Clock,
    policy: ContentAuthorizationPolicy,
    actor: ContentActor,
    max_bytes: int,
) -> MediaBlob:
    """Authorize, size-check, process, store, then record one media blob.

    Bytes are saved before the record so a record never points at missing
    bytes; if recording fails the saved file is deleted and the error re-raised.
    """
    policy.authorize(actor, ContentAction.UPLOAD_MEDIA)
    if len(command.data) > max_bytes:
        raise UnsupportedMediaError("media_too_large")
    processed = processor.process(
        command.data, preserve_original=command.preserve_original
    )
    blob_id = ids.new_uuid()
    blob = MediaBlob(
        id=blob_id,
        storage_key=media_storage_key(blob_id, processed.extension),
        sha256=hashlib.sha256(processed.data).hexdigest(),
        content_type=processed.content_type,
        size_bytes=len(processed.data),
        width=processed.width,
        height=processed.height,
        created_at=clock.now(),
        created_by=command.actor_id,
    )
    files.save(blob.storage_key, processed.data)
    try:
        blobs.add(blob)
    except BaseException:
        files.delete(blob.storage_key)
        raise
    return blob
