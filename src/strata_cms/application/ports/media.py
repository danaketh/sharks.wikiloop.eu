"""Ports for media processing, byte storage, and blob metadata persistence."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from strata_cms.domain.media import MediaBlob


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    """Validated image bytes plus the type/extension sniffed from content."""

    data: bytes
    content_type: str
    extension: str
    width: int
    height: int


class ImageProcessor(Protocol):
    """Sniff, validate and (by default) metadata-strip untrusted image bytes."""

    def process(self, data: bytes, *, preserve_original: bool) -> ProcessedImage:
        """Return processed bytes or raise `UnsupportedMediaError`.

        The type is determined from content only. With ``preserve_original``
        the original bytes are returned unchanged but still fully validated.
        """
        ...  # pragma: no cover - protocol declaration


class MediaFileStore(Protocol):
    """Store media bytes under server-generated keys."""

    def save(self, key: str, data: bytes) -> None:
        """Store ``data`` at exactly ``key``; fail rather than rename."""
        ...  # pragma: no cover - protocol declaration

    def url(self, key: str) -> str:
        """Return a client-usable URL for the stored bytes."""
        ...  # pragma: no cover - protocol declaration

    def delete(self, key: str) -> None:
        """Delete the stored bytes; a missing key is not an error."""
        ...  # pragma: no cover - protocol declaration


class MediaBlobRepository(Protocol):
    """Persist and load media blob metadata entities."""

    def add(self, blob: MediaBlob) -> None:
        """Insert a new blob."""
        ...  # pragma: no cover - protocol declaration

    def get_many(self, ids: frozenset[UUID]) -> Mapping[UUID, MediaBlob]:
        """Return the existing blobs among ``ids``."""
        ...  # pragma: no cover - protocol declaration

    def list_orphans(
        self, *, created_before: datetime, limit: int
    ) -> tuple[MediaBlob, ...]:
        """Return up to ``limit`` unused blobs created before the cutoff."""
        ...  # pragma: no cover - protocol declaration

    def delete_if_unused(self, blob_id: UUID) -> bool:
        """Atomically delete one blob's metadata only while no revision uses it.

        Returns whether a row was deleted; a used or missing blob is kept/no-op.
        Implementations must serialize with concurrent blob-usage writes so a
        revision committed meanwhile is never left pointing at a deleted blob.
        """
        ...  # pragma: no cover - protocol declaration
